"""Integracao segura com Microsoft Teams por webhook/Workflow."""
from __future__ import annotations

import json
import logging
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from config import settings

logger = logging.getLogger("geodemandas.teams")


def send_teams_notification(title: str, message: str, *, severity: str = "info", url: str | None = None) -> bool:
    webhook = (url or settings.TEAMS_WEBHOOK_URL).strip()
    if not webhook:
        return False
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or not parsed.hostname:
        logger.error("TEAMS_WEBHOOK_URL deve ser uma URL HTTPS valida")
        return False
    color = {"critical": "attention", "warning": "warning", "resolved": "good"}.get(severity, "accent")
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "color": color, "wrap": True},
                    {"type": "TextBlock", "text": message, "wrap": True},
                ],
                "actions": [{"type": "Action.OpenUrl", "title": "Abrir GeoDemandas", "url": settings.APP_BASE_URL}],
            },
        }],
    }
    try:
        request = Request(webhook, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except Exception:
        logger.exception("Falha ao enviar alerta ao Microsoft Teams")
        return False
