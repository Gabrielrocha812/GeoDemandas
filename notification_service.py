"""Notificações transacionais via Microsoft Graph ou SMTP."""
from __future__ import annotations

import html
import json
import logging
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from config import settings

logger = logging.getLogger("geodemandas.notifications")
_token_lock = Lock()
_token_cache: dict[str, str | float] = {"access_token": "", "expires_at": 0.0}


def send_ticket_received(
    recipient: str,
    recipient_name: str,
    ticket_id: int,
    subject: str,
    priority: str,
) -> bool:
    return _send(
        recipient,
        f"Demanda #{ticket_id:04d} recebida",
        recipient_name,
        ticket_id,
        subject,
        "Recebemos sua demanda",
        (
            "Sua solicitação foi registrada com sucesso e já está disponível "
            f"para acompanhamento. Prioridade inicial: <strong>{html.escape(priority)}</strong>."
        ),
        "Recebida",
    )


def send_ticket_update(
    recipient: str,
    recipient_name: str,
    ticket_id: int,
    subject: str,
    update_text: str,
) -> bool:
    return _send(
        recipient,
        f"Atualização na demanda #{ticket_id:04d}",
        recipient_name,
        ticket_id,
        subject,
        "A demanda foi atualizada",
        html.escape(update_text),
        "Atualizada",
    )


def send_ticket_completed(
    recipient: str,
    recipient_name: str,
    ticket_id: int,
    subject: str,
) -> bool:
    return _send(
        recipient,
        f"Demanda #{ticket_id:04d} concluída",
        recipient_name,
        ticket_id,
        subject,
        "Sua demanda foi concluída",
        (
            "A equipe finalizou o atendimento. Você pode acessar o histórico "
            "completo pelo botão abaixo."
        ),
        "Concluída",
    )


def _send(
    recipient: str,
    email_subject: str,
    recipient_name: str,
    ticket_id: int,
    ticket_subject: str,
    heading: str,
    message_html: str,
    badge: str,
) -> bool:
    ticket_url = f"{settings.APP_BASE_URL.rstrip('/')}/tickets/{ticket_id}"
    safe_name = html.escape(recipient_name or "Solicitante")
    safe_subject = html.escape(ticket_subject)
    body = f"""\
<!doctype html>
<html lang="pt-BR">
<body style="margin:0;background:#f2f7f4;font-family:Arial,sans-serif;color:#173126">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:32px 16px">
    <tr><td align="center">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:620px;background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 18px 55px rgba(0,80,43,.12)">
        <tr><td style="padding:28px 32px;background:linear-gradient(135deg,#005f31,#16b364);color:#fff">
          <div style="font-size:20px;font-weight:800">GeoDemandas</div>
          <div style="margin-top:4px;font-size:11px;letter-spacing:2px;text-transform:uppercase;opacity:.8">Brandt Intelligence</div>
        </td></tr>
        <tr><td style="padding:36px 32px">
          <span style="display:inline-block;padding:7px 12px;border-radius:999px;background:#eaf8f0;color:#00723b;font-size:11px;font-weight:800;text-transform:uppercase">{html.escape(badge)}</span>
          <h1 style="margin:22px 0 10px;font-size:26px;line-height:1.2">{html.escape(heading)}</h1>
          <p style="margin:0 0 22px;color:#607068;font-size:15px;line-height:1.7">Olá, {safe_name}. {message_html}</p>
          <div style="padding:18px;border:1px solid #e3eee8;border-radius:14px;background:#f8fbf9">
            <div style="font-size:11px;font-weight:800;color:#008542;text-transform:uppercase;letter-spacing:1px">Demanda #{ticket_id:04d}</div>
            <div style="margin-top:7px;font-size:16px;font-weight:700">{safe_subject}</div>
          </div>
          <a href="{html.escape(ticket_url)}" style="display:inline-block;margin-top:24px;padding:14px 22px;border-radius:12px;background:#008542;color:#fff;text-decoration:none;font-size:14px;font-weight:800">Acompanhar demanda</a>
        </td></tr>
        <tr><td style="padding:20px 32px;border-top:1px solid #edf2ef;color:#87928c;font-size:11px">Mensagem automática do GeoDemandas Brandt.</td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    provider = settings.EMAIL_PROVIDER.strip().lower()
    if provider == "graph":
        return _send_graph(recipient, email_subject, body)
    if provider == "smtp":
        return _send_smtp(
            recipient,
            email_subject,
            heading,
            ticket_id,
            ticket_subject,
            ticket_url,
            body,
        )
    logger.error("EMAIL_PROVIDER inválido: %s", settings.EMAIL_PROVIDER)
    return False


def _send_smtp(
    recipient: str,
    email_subject: str,
    heading: str,
    ticket_id: int,
    ticket_subject: str,
    ticket_url: str,
    body: str,
) -> bool:
    if not settings.SMTP_ENABLED:
        logger.info(
            "[SMTP DESABILITADO] Notificação '%s' destinada a %s",
            email_subject,
            recipient,
        )
        return False
    if not settings.SMTP_HOST:
        logger.error("SMTP_ENABLED=true, mas SMTP_HOST não foi configurado")
        return False

    msg = EmailMessage()
    msg["Subject"] = email_subject
    msg["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_EMAIL))
    msg["To"] = recipient
    msg.set_content(
        f"{heading}\n\nDemanda #{ticket_id:04d}: {ticket_subject}\n{ticket_url}"
    )
    msg.add_alternative(body, subtype="html")

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as client:
            if settings.SMTP_USE_TLS:
                client.starttls()
            if settings.SMTP_USERNAME:
                client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            client.send_message(msg)
        logger.info("Notificação enviada para %s: %s", recipient, email_subject)
        return True
    except Exception:
        logger.exception("Falha ao enviar notificação para %s", recipient)
        return False


def _send_graph(recipient: str, email_subject: str, html_body: str) -> bool:
    if not all(
        [
            settings.GRAPH_TENANT_ID,
            settings.GRAPH_CLIENT_ID,
            settings.GRAPH_CLIENT_SECRET,
            settings.GRAPH_SENDER_EMAIL,
        ]
    ):
        logger.warning(
            "[GRAPH INCOMPLETO] Notificação '%s' destinada a %s não enviada",
            email_subject,
            recipient,
        )
        return False

    try:
        token = _graph_access_token()
        sender = quote(settings.GRAPH_SENDER_EMAIL, safe="@.")
        url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
        payload = {
            "message": {
                "subject": email_subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [
                    {"emailAddress": {"address": recipient}}
                ],
            },
            "saveToSentItems": True,
        }
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=25) as response:
            if response.status != 202:
                raise RuntimeError(
                    f"Microsoft Graph retornou HTTP {response.status}"
                )
        logger.info(
            "Notificação enviada via Graph para %s: %s",
            recipient,
            email_subject,
        )
        return True
    except HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")[:800]
        logger.error(
            "Microsoft Graph recusou o envio (HTTP %s): %s",
            exc.code,
            response_text,
        )
    except (URLError, TimeoutError, RuntimeError, ValueError):
        logger.exception("Falha no envio via Microsoft Graph para %s", recipient)
    return False


def _graph_access_token() -> str:
    with _token_lock:
        now = time.time()
        cached = str(_token_cache["access_token"])
        if cached and float(_token_cache["expires_at"]) > now + 60:
            return cached

        token_url = (
            "https://login.microsoftonline.com/"
            f"{quote(settings.GRAPH_TENANT_ID, safe='')}/oauth2/v2.0/token"
        )
        form = urlencode(
            {
                "client_id": settings.GRAPH_CLIENT_ID,
                "client_secret": settings.GRAPH_CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            }
        ).encode("utf-8")
        request = Request(
            token_url,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        token = data.get("access_token")
        if not token:
            raise ValueError("Resposta do Entra ID não contém access_token")
        _token_cache["access_token"] = token
        _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
        return str(token)
