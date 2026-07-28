"""
email_worker.py
---------------
Worker assíncrono de segundo plano que monitora a caixa
`geodemandas@brandt.com.br` via IMAP.

Fluxo a cada ciclo (a cada EMAIL_POLL_INTERVAL segundos):
  1. Conecta na caixa IMAP e busca e-mails NÃO LIDOS.
  2. Para cada e-mail, extrai remetente, assunto e corpo.
  3. Valida o remetente no AD via ldap_auth.validate_sender().
  4. Se válido e ativo -> garante o usuário no banco e cria o Ticket.
     Se inválido -> ignora (loga o motivo).
  5. Marca o e-mail como lido para não reprocessar.

Em DEV_MODE, ao invés de IMAP real, o worker "injeta" e-mails fictícios uma
única vez, permitindo testar todo o pipeline sem servidor de e-mail.

A operação de IMAP é bloqueante (imaplib), então rodamos em um thread pool
via `asyncio.to_thread` para não travar o event loop do FastAPI.
"""
from __future__ import annotations

import asyncio
import email
import logging
from email.header import decode_header
from email.utils import parseaddr

from config import settings
from database import SessionLocal, Ticket, TicketPriority, TicketStatus, User
from ldap_auth import validate_sender
from notification_service import send_ticket_received

logger = logging.getLogger("geodemandas.worker")

# Flag para permitir parada limpa do loop no shutdown.
_running = False


# --------------------------------------------------------------------------
# Loop principal
# --------------------------------------------------------------------------
async def email_worker_loop() -> None:
    """Loop infinito agendado no startup do FastAPI (lifespan)."""
    global _running
    _running = True
    logger.info(
        "Worker de e-mail iniciado (DEV_MODE=%s, intervalo=%ss)",
        settings.DEV_MODE,
        settings.EMAIL_POLL_INTERVAL,
    )

    if settings.DEV_MODE:
        # Em dev, injeta e-mails fictícios uma vez e mantém o loop ocioso.
        await asyncio.sleep(3)  # dá tempo do servidor subir
        await asyncio.to_thread(_process_mock_emails)

    while _running:
        try:
            if not settings.DEV_MODE:
                # to_thread evita bloquear o event loop com imaplib síncrono.
                await asyncio.to_thread(_poll_imap_once)
        except Exception as exc:  # noqa: BLE001 (worker resiliente)
            logger.exception("Erro no ciclo do worker: %s", exc)
        await asyncio.sleep(settings.EMAIL_POLL_INTERVAL)


def stop_worker() -> None:
    global _running
    _running = False


# --------------------------------------------------------------------------
# Processamento IMAP real
# --------------------------------------------------------------------------
def _poll_imap_once() -> None:
    import imaplib

    if settings.IMAP_USE_SSL:
        client = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT)
    else:
        client = imaplib.IMAP4(settings.IMAP_HOST, settings.IMAP_PORT)

    try:
        client.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
        client.select(settings.IMAP_MAILBOX)

        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            logger.warning("Busca IMAP falhou: %s", status)
            return

        message_ids = data[0].split()
        logger.info("%d e-mail(s) não lido(s) encontrado(s)", len(message_ids))

        for num in message_ids:
            status, msg_data = client.fetch(num, "(RFC822)")
            if status != "OK":
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            _handle_message(msg)
            # Marca como lido para não reprocessar
            client.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        client.logout()


# --------------------------------------------------------------------------
# Núcleo compartilhado: transforma um EmailMessage em Ticket
# --------------------------------------------------------------------------
def _handle_message(msg: email.message.Message) -> None:
    sender_email = parseaddr(msg.get("From", ""))[1].lower()
    subject = _decode_mime(msg.get("Subject", "(sem assunto)"))
    message_id = msg.get("Message-ID")
    body = _extract_body(msg)

    _create_ticket_from_email(sender_email, subject, body, message_id)


def _create_ticket_from_email(
    sender_email: str, subject: str, body: str, message_id: str | None
) -> None:
    """Valida remetente no AD e cria o ticket. Retorna silenciosamente se inválido."""
    # 1) Validação no Active Directory
    ad_user = validate_sender(sender_email)
    if not ad_user:
        logger.info("E-mail rejeitado (remetente não validado no AD): %s", sender_email)
        return

    db = SessionLocal()
    try:
        # 2) Evita duplicidade pelo Message-ID
        if message_id:
            exists = db.query(Ticket).filter(Ticket.source_message_id == message_id).first()
            if exists:
                logger.info("E-mail já processado (Message-ID duplicado): %s", message_id)
                return

        # 3) Garante o usuário no banco (sincroniza do AD se necessário)
        user = db.query(User).filter(User.email == sender_email).first()
        if not user:
            user = User(
                email=sender_email,
                full_name=ad_user["full_name"],
                department=ad_user.get("department"),
                is_technician=False,
                is_active=True,
            )
            db.add(user)
            db.flush()  # garante user.id

        # 4) Cria o ticket
        ticket = Ticket(
            subject=subject.strip() or "(sem assunto)",
            body=body.strip() or "(e-mail sem corpo)",
            status=TicketStatus.ABERTO,
            priority=_guess_priority(subject, body),
            source_message_id=message_id,
            requester_id=user.id,
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        logger.info("Ticket #%s criado para %s", ticket.id, sender_email)
        send_ticket_received(
            user.email,
            user.full_name,
            ticket.id,
            ticket.subject,
            ticket.priority.value,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Falha ao criar ticket de %s: %s", sender_email, exc)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Utilidades de parsing de e-mail
# --------------------------------------------------------------------------
def _decode_mime(value: str) -> str:
    """Decodifica cabeçalhos MIME (ex.: assuntos com acento/UTF-8)."""
    parts = decode_header(value)
    decoded = ""
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded += text.decode(charset or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def _extract_body(msg: email.message.Message) -> str:
    """Extrai o corpo em texto puro do e-mail (multipart ou simples)."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # fallback: primeiro text/html
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return str(msg.get_payload())


def _guess_priority(subject: str, body: str) -> TicketPriority:
    """Heurística simples de prioridade por palavras-chave no assunto/corpo."""
    text = f"{subject} {body}".lower()
    if any(k in text for k in ("urgente", "urgência", "parado", "crítico", "critico")):
        return TicketPriority.URGENTE
    if any(k in text for k in ("importante", "prioridade", "asap")):
        return TicketPriority.ALTA
    return TicketPriority.MEDIA


# --------------------------------------------------------------------------
# Simulação para DEV_MODE
# --------------------------------------------------------------------------
def _process_mock_emails() -> None:
    """
    Injeta e-mails fictícios para testar o pipeline sem IMAP real.
    Um deles é de remetente NÃO cadastrado no AD (será rejeitado).
    """
    logger.info("[DEV] Injetando e-mails fictícios para teste do worker...")
    fake_emails = [
        {
            "from": "joao.silva@brandt.com.br",
            "subject": "URGENTE: Erro no cálculo de coordenadas do projeto Alpha",
            "body": (
                "Olá equipe,\n\nO sistema de geolocalização está retornando "
                "coordenadas incorretas para o Projeto Alpha desde ontem. "
                "Isso está travando a entrega. Podem verificar com urgência?\n\n"
                "Obrigado,\nJoão Silva - Geotecnia"
            ),
            "message_id": "<mock-001@brandt.com.br>",
        },
        {
            "from": "maria.souza@brandt.com.br",
            "subject": "Solicitação de acesso ao módulo de relatórios ambientais",
            "body": (
                "Boa tarde,\n\nGostaria de solicitar acesso ao módulo de "
                "relatórios ambientais no GeoDemandas. Preciso gerar o "
                "relatório mensal de licenciamento.\n\nAtenciosamente,\nMaria Souza"
            ),
            "message_id": "<mock-002@brandt.com.br>",
        },
        {
            "from": "externo@gmail.com",  # NÃO existe no AD -> deve ser rejeitado
            "subject": "Proposta comercial imperdível",
            "body": "Conheça nossos serviços...",
            "message_id": "<mock-003@external.com>",
        },
    ]
    for fe in fake_emails:
        _create_ticket_from_email(fe["from"], fe["subject"], fe["body"], fe["message_id"])
