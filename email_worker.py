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
  5. Marca o e-mail como lido somente após sucesso ou rejeição definitiva.
     Falhas transitórias permanecem não lidas para uma nova tentativa.

Em DEV_MODE, ao invés de IMAP real, o worker "injeta" e-mails fictícios uma
única vez, permitindo testar todo o pipeline sem servidor de e-mail.

A operação de IMAP é bloqueante (imaplib), então rodamos em um thread pool
via `asyncio.to_thread` para não travar o event loop do FastAPI.
"""
from __future__ import annotations

import asyncio
import email
import hashlib
import io
import logging
import re
from enum import Enum
from email.header import decode_header
from email.utils import parseaddr

from audit_service import record_event
from attachment_service import delete_saved_uploads, save_uploads
from config import settings
from database import Comment, SessionLocal, Ticket, TicketPriority, TicketStatus, User
from fastapi import UploadFile
from ldap_auth import LDAPOperationalError, validate_sender
from outbox_service import enqueue_ticket_received, enqueue_ticket_update
from operational_health import beat
from workflow_service import handle_requester_reply, initialize_sla

logger = logging.getLogger("geodemandas.worker")

# Flag para permitir parada limpa do loop no shutdown.
_running = False


class EmailProcessingResult(str, Enum):
    """Resultado que determina se a mensagem pode sair da fila IMAP."""

    SUCCESS = "success"
    PERMANENT_REJECTION = "permanent_rejection"
    TRANSIENT_FAILURE = "transient_failure"


_FINAL_RESULTS = {
    EmailProcessingResult.SUCCESS,
    EmailProcessingResult.PERMANENT_REJECTION,
}


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
            # Heartbeat significa ciclo concluído, não apenas processo vivo.
            beat("email")
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
        status, _ = client.select(settings.IMAP_MAILBOX)
        if status != "OK":
            logger.warning("Seleção da caixa IMAP falhou: %s", status)
            return

        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            logger.warning("Busca IMAP falhou: %s", status)
            return

        message_ids = data[0].split()
        logger.info("%d e-mail(s) não lido(s) encontrado(s)", len(message_ids))

        for num in message_ids:
            try:
                # BODY.PEEK evita que o próprio FETCH aplique \Seen antes de
                # sabermos se o processamento terminou de forma definitiva.
                status, msg_data = client.fetch(num, "(BODY.PEEK[])")
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Falha transitória ao buscar mensagem IMAP %r; "
                    "mensagem mantida não lida",
                    num,
                )
                continue
            if status != "OK" or not msg_data:
                logger.warning(
                    "Busca da mensagem IMAP %r falhou (%s); "
                    "mensagem mantida não lida",
                    num,
                    status,
                )
                continue

            raw = next(
                (
                    item[1]
                    for item in msg_data
                    if isinstance(item, tuple)
                    and len(item) > 1
                    and isinstance(item[1], bytes)
                ),
                None,
            )
            if raw is None:
                logger.warning(
                    "Mensagem IMAP %r sem conteúdo RFC822; "
                    "mensagem mantida não lida",
                    num,
                )
                continue

            try:
                msg = email.message_from_bytes(raw)
                result = _handle_message(msg)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Falha transitória inesperada ao processar mensagem IMAP %r; "
                    "mensagem mantida não lida",
                    num,
                )
                result = EmailProcessingResult.TRANSIENT_FAILURE

            if result not in _FINAL_RESULTS:
                logger.warning(
                    "Mensagem IMAP %r mantida não lida para nova tentativa",
                    num,
                )
                continue

            try:
                store_status, _ = client.store(num, "+FLAGS", "\\Seen")
                if store_status != "OK":
                    logger.warning(
                        "Não foi possível marcar mensagem IMAP %r como lida (%s)",
                        num,
                        store_status,
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Não foi possível marcar mensagem IMAP %r como lida",
                    num,
                )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Núcleo compartilhado: transforma um EmailMessage em Ticket
# --------------------------------------------------------------------------
def _handle_message(msg: email.message.Message) -> EmailProcessingResult:
    sender_email = parseaddr(msg.get("From", ""))[1].lower()
    if not sender_email:
        logger.info("E-mail rejeitado permanentemente: remetente ausente")
        return EmailProcessingResult.PERMANENT_REJECTION

    subject = _decode_mime(msg.get("Subject", "(sem assunto)"))
    message_id = msg.get("Message-ID")
    if not message_id:
        # Alguns equipamentos e integrações omitem Message-ID. O hash da
        # mensagem mantém a criação idempotente caso o ACK IMAP falhe.
        digest = hashlib.sha256(msg.as_bytes()).hexdigest()
        message_id = f"<sha256-{digest}@geodemandas.local>"
    body = _extract_body(msg)
    return _create_ticket_from_email(
        sender_email,
        subject,
        body,
        message_id,
        in_reply_to=msg.get("In-Reply-To"),
        references=msg.get("References"),
        attachments=_extract_attachments(msg),
    )


def _create_ticket_from_email(
    sender_email: str,
    subject: str,
    body: str,
    message_id: str | None,
    *,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[UploadFile] | None = None,
) -> EmailProcessingResult:
    """Valida o remetente e retorna um resultado explícito do processamento."""
    # 1) Validação no Active Directory
    try:
        ad_user = validate_sender(sender_email)
    except LDAPOperationalError:
        logger.warning(
            "Falha transitória ao validar remetente no diretório; "
            "e-mail será tentado novamente"
        )
        return EmailProcessingResult.TRANSIENT_FAILURE
    except Exception:  # noqa: BLE001
        logger.exception(
            "Falha transitória inesperada durante validação do remetente; "
            "e-mail será tentado novamente"
        )
        return EmailProcessingResult.TRANSIENT_FAILURE

    if not ad_user:
        logger.info(
            "E-mail rejeitado permanentemente "
            "(remetente inexistente ou inativo no AD): %s",
            sender_email,
        )
        return EmailProcessingResult.PERMANENT_REJECTION

    db = None
    ticket = None
    user = None
    saved_attachments = []
    try:
        db = SessionLocal()

        # 2) Evita duplicidade pelo Message-ID
        if message_id:
            exists = db.query(Ticket).filter(Ticket.source_message_id == message_id).first()
            existing_comment = (
                db.query(Comment)
                .filter(Comment.source_message_id == message_id)
                .first()
            )
            if exists or existing_comment:
                logger.info("E-mail já processado (Message-ID duplicado): %s", message_id)
                return EmailProcessingResult.SUCCESS

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

        thread_ticket = _find_thread_ticket(
            db, subject, in_reply_to=in_reply_to, references=references
        )
        if thread_ticket is not None:
            if thread_ticket.requester_id != user.id:
                logger.warning(
                    "Resposta por e-mail rejeitada: remetente sem acesso Ã  demanda #%s",
                    thread_ticket.id,
                )
                return EmailProcessingResult.PERMANENT_REJECTION
            comment = Comment(
                ticket_id=thread_ticket.id,
                author_id=user.id,
                content=_clean_reply_body(body) or "Anexo enviado por e-mail.",
                is_system=False,
                is_internal=False,
                source_message_id=message_id,
            )
            db.add(comment)
            db.flush()
            saved_attachments = save_uploads(
                db, thread_ticket, user, attachments or [], comment=comment
            )
            status_message = handle_requester_reply(thread_ticket)
            record_event(
                db,
                "ticket.comment.public_added",
                actor=user,
                ticket=thread_ticket,
                summary="Resposta recebida pela caixa de e-mail.",
                changes={
                    "comment_id": comment.id,
                    "attachment_count": len(saved_attachments),
                    "source_channel": "email",
                },
                source="imap",
            )
            if thread_ticket.assignee:
                enqueue_ticket_update(
                    db,
                    thread_ticket.assignee.email,
                    thread_ticket.assignee.full_name,
                    thread_ticket.id,
                    thread_ticket.subject,
                    "O solicitante respondeu por e-mail.",
                    dedupe_key=f"email-reply:{message_id}",
                )
            if status_message:
                db.add(
                    Comment(
                        ticket_id=thread_ticket.id,
                        author_id=user.id,
                        content=status_message,
                        is_system=True,
                        is_internal=False,
                    )
                )
            db.commit()
            logger.info("Resposta anexada Ã  demanda #%s", thread_ticket.id)
            return EmailProcessingResult.SUCCESS

        # 4) Cria o ticket
        ticket = Ticket(
            subject=subject.strip() or "(sem assunto)",
            body=body.strip() or "(e-mail sem corpo)",
            status=TicketStatus.ABERTO,
            priority=_guess_priority(subject, body),
            source_channel="email",
            source_message_id=message_id,
            requester_id=user.id,
        )
        initialize_sla(ticket)
        db.add(ticket)
        db.flush()
        saved_attachments = save_uploads(db, ticket, user, attachments or [])
        record_event(
            db,
            "ticket.created",
            ticket=ticket,
            summary="Demanda criada pela caixa de e-mail.",
            changes={
                "status": ticket.status,
                "priority": ticket.priority,
                "source_channel": ticket.source_channel,
                "attachment_count": len(saved_attachments),
            },
            source="imap",
            actor_type="worker",
        )
        enqueue_ticket_received(
            db,
            user.email,
            user.full_name,
            ticket.id,
            ticket.subject,
            ticket.priority.value,
            dedupe_key=f"ticket-received:{ticket.id}",
        )
        db.commit()
        db.refresh(ticket)
        logger.info("Ticket #%s criado para %s", ticket.id, sender_email)
    except Exception:  # noqa: BLE001
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("Falha adicional ao desfazer transação do worker")
        logger.exception(
            "Falha transitória ao criar ticket para %s; "
            "e-mail será tentado novamente",
            sender_email,
        )
        delete_saved_uploads(saved_attachments)
        return EmailProcessingResult.TRANSIENT_FAILURE
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                logger.exception("Falha ao fechar sessão do worker")

    return EmailProcessingResult.SUCCESS


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


def _extract_attachments(msg: email.message.Message) -> list[UploadFile]:
    """Converte anexos MIME para o serviÃ§o compartilhado de uploads."""
    uploads: list[UploadFile] = []
    if not msg.is_multipart():
        return uploads
    for part in msg.walk():
        filename = part.get_filename()
        if not filename or (part.get_content_disposition() or "").lower() not in {"attachment", "inline"}:
            continue
        uploads.append(
            UploadFile(
                filename=_decode_mime(filename),
                file=io.BytesIO(part.get_payload(decode=True) or b""),
                headers={"content-type": part.get_content_type()},
            )
        )
    return uploads


def _clean_reply_body(body: str) -> str:
    """Remove histórico citado e assinaturas comuns sem alterar a mensagem original."""
    kept = []
    for line in body.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        lowered = stripped.casefold()
        if stripped == "--" or lowered.startswith("de:") or lowered.startswith("from:") or lowered.startswith("em ") and lowered.endswith(" escreveu:"):
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


_TICKET_TOKEN_RE = re.compile(
    r"\[#?(\d{1,10})\]|(?:demanda\s*)?#(\d{1,10})", re.IGNORECASE
)
_MESSAGE_ID_RE = re.compile(r"<[^>]+>")


def _find_thread_ticket(
    db, subject: str, *, in_reply_to: str | None, references: str | None
) -> Ticket | None:
    """Resolve a conversa por token no assunto ou cabeçalhos RFC 5322."""
    match = _TICKET_TOKEN_RE.search(subject)
    if match:
        ticket_id = int(match.group(1) or match.group(2))
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket is not None:
            return ticket
    message_ids = _MESSAGE_ID_RE.findall(f"{in_reply_to or ''} {references or ''}")
    if not message_ids:
        return None
    return (
        db.query(Ticket)
        .filter(Ticket.source_message_id.in_(message_ids))
        .first()
    )


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
