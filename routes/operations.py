"""Painel administrativo de confiabilidade, notificacoes e auditoria."""
from __future__ import annotations

import math
from datetime import timedelta
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import Session

from audit_service import record_event
from auth import ROLE_ADMIN, require_roles
from database import (
    AuditEvent,
    NotificationOutbox,
    Ticket,
    User,
    get_db,
    utcnow,
)
from routes.web import templates
from time_utils import as_local
from workflow_service import RESOLUTION_STATUSES, sla_snapshot

router = APIRouter()

PAGE_SIZE = 25
MAX_SEARCH_LENGTH = 120
DELIVERY_STATUSES = {
    "pending": "Pendente",
    "sent": "Enviado",
    "failed": "Falhou",
}
EVENT_TYPES = {
    "ticket_received": "Demanda recebida",
    "ticket_update": "Atualizacao da demanda",
    "ticket_completed": "Demanda concluida",
    "sla_warning": "Alerta de SLA",
    "sla_overdue": "SLA vencido",
}
AUDIT_ACTION_LABELS = {
    "ticket.created": "criou uma demanda",
    "ticket.comment.public_added": "adicionou uma mensagem publica",
    "ticket.comment.internal_added": "adicionou uma nota interna",
    "ticket.status.changed": "alterou o status",
    "ticket.status.auto_changed": "teve o status atualizado automaticamente",
    "ticket.assignee.changed": "alterou o responsavel",
    "ticket.metadata.changed": "alterou dados operacionais",
    "ticket.attachment.downloaded": "baixou um anexo",
    "user.role.changed": "alterou um perfil de acesso",
    "ldap.sync.completed": "sincronizou usuarios do diretorio",
    "notification.retry_requested": "solicitou nova tentativa de envio",
    "notification.delivery_failed": "registrou uma falha definitiva de envio",
    "ticket.sla.alert_queued": "enfileirou um alerta de SLA",
}


def _safe_filter(value: str | None, allowed: dict[str, str]) -> str:
    clean = (value or "").strip()
    return clean if clean in allowed else ""


def _delivery_query(
    db: Session,
    *,
    search_query: str,
    status_filter: str,
    event_filter: str,
):
    query = db.query(NotificationOutbox)
    if search_query:
        like_value = f"%{search_query}%"
        query = query.filter(
            or_(
                NotificationOutbox.recipient.ilike(like_value),
                NotificationOutbox.last_error.ilike(like_value),
                NotificationOutbox.event_type.ilike(like_value),
                cast(NotificationOutbox.ticket_id, String).ilike(like_value),
            )
        )
    if status_filter:
        query = query.filter(NotificationOutbox.status == status_filter)
    if event_filter:
        query = query.filter(NotificationOutbox.event_type == event_filter)
    return query


def _ticket_metrics(db: Session, now) -> tuple[int, int, int]:
    active_tickets = (
        db.query(Ticket)
        .filter(Ticket.status.notin_(tuple(RESOLUTION_STATUSES)))
        .all()
    )
    risk = 0
    overdue = 0
    unassigned = 0
    for ticket in active_tickets:
        if ticket.assignee_id is None:
            unassigned += 1
        state = sla_snapshot(ticket, now=now)["state"]
        if state == "risk":
            risk += 1
        elif state == "overdue":
            overdue += 1
    return risk, overdue, unassigned


def _daily_delivery_series(db: Session, now) -> list[SimpleNamespace]:
    local_now = as_local(now)
    first_day = (
        (local_now.date() if local_now is not None else now.date())
        - timedelta(days=6)
    )
    values: dict = {
        first_day + timedelta(days=offset): {"sent": 0, "failed": 0}
        for offset in range(7)
    }
    rows = (
        db.query(NotificationOutbox)
        .filter(
            NotificationOutbox.updated_at >= now - timedelta(days=7),
            NotificationOutbox.status.in_(["sent", "failed"]),
        )
        .all()
    )
    if not rows:
        return []
    for row in rows:
        timestamp = row.sent_at if row.status == "sent" else row.updated_at
        local_timestamp = as_local(timestamp)
        if local_timestamp is None:
            continue
        bucket = values.get(local_timestamp.date())
        if bucket is not None:
            bucket[row.status] += 1
    return [
        SimpleNamespace(
            label=day.strftime("%d/%m"),
            sent=counts["sent"],
            failed=counts["failed"],
        )
        for day, counts in sorted(values.items())
    ]


def _recent_audit_events(db: Session) -> list[SimpleNamespace]:
    events = (
        db.query(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(12)
        .all()
    )
    return [
        SimpleNamespace(
            actor_name=event.actor_name or "Sistema",
            action=AUDIT_ACTION_LABELS.get(
                event.action,
                event.action.replace(".", " "),
            ),
            summary=event.summary,
            ticket_id=event.ticket_id,
            created_at=event.created_at,
        )
        for event in events
    ]


@router.get("/admin/operacao", response_class=HTMLResponse)
def operations_dashboard(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    event: str | None = None,
    page: int = 1,
    current_user: User = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    search_query = (q or "").strip()[:MAX_SEARCH_LENGTH]
    status_filter = _safe_filter(status, DELIVERY_STATUSES)
    event_filter = _safe_filter(event, EVENT_TYPES)
    page = max(1, page)

    query = _delivery_query(
        db,
        search_query=search_query,
        status_filter=status_filter,
        event_filter=event_filter,
    )
    total_items = query.count()
    total_pages = max(1, math.ceil(total_items / PAGE_SIZE))
    page = min(page, total_pages)
    deliveries = (
        query.order_by(
            NotificationOutbox.created_at.desc(),
            NotificationOutbox.id.desc(),
        )
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    now = utcnow()
    risk, overdue, unassigned = _ticket_metrics(db, now)
    metrics = SimpleNamespace(
        pending=(
            db.query(func.count(NotificationOutbox.id))
            .filter(NotificationOutbox.status == "pending")
            .scalar()
            or 0
        ),
        sent_24h=(
            db.query(func.count(NotificationOutbox.id))
            .filter(
                NotificationOutbox.status == "sent",
                NotificationOutbox.sent_at >= now - timedelta(hours=24),
            )
            .scalar()
            or 0
        ),
        failed=(
            db.query(func.count(NotificationOutbox.id))
            .filter(NotificationOutbox.status == "failed")
            .scalar()
            or 0
        ),
        sla_risk=risk,
        sla_overdue=overdue,
        unassigned=unassigned,
    )

    return templates.TemplateResponse(
        request,
        "admin_operations.html",
        {
            "current_user": current_user,
            "metrics": metrics,
            "deliveries": deliveries,
            "audit_events": _recent_audit_events(db),
            "daily_series": _daily_delivery_series(db, now),
            "active_status": status_filter,
            "active_event": event_filter,
            "search_query": search_query,
            "page": page,
            "total_pages": total_pages,
            "total_items": total_items,
            "status_options": [
                SimpleNamespace(value=value, label=label)
                for value, label in DELIVERY_STATUSES.items()
            ],
            "event_options": [
                SimpleNamespace(value=value, label=label)
                for value, label in EVENT_TYPES.items()
            ],
        },
    )


@router.post("/admin/operacao/notificacoes/{notification_id}/repetir")
def retry_notification(
    notification_id: int,
    current_user: User = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    notification = db.get(NotificationOutbox, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notificacao nao encontrada")
    if notification.status != "failed":
        raise HTTPException(
            status_code=409,
            detail="Somente notificacoes com falha podem ser reenfileiradas",
        )

    notification.status = "pending"
    notification.attempts = 0
    notification.next_attempt_at = utcnow()
    notification.locked_at = None
    notification.lock_token = None
    notification.sent_at = None
    notification.last_error = None
    notification.updated_at = utcnow()
    record_event(
        db,
        "notification.retry_requested",
        actor=current_user,
        ticket_id=notification.ticket_id,
        summary=f"Entrega #{notification.id} reenfileirada para nova tentativa.",
        changes={
            "notification_id": notification.id,
            "before": {"status": "failed"},
            "after": {"status": "pending", "attempts": 0},
        },
        source="web",
        category="system",
        resource_type="notification",
        resource_id=str(notification.id),
    )
    db.commit()
    return RedirectResponse(
        "/admin/operacao?status=failed&repetida=1",
        status_code=303,
    )
