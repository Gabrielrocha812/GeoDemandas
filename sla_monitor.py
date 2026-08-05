"""Monitor persistente de alertas operacionais de SLA.

O monitor apenas enfileira notificacoes na outbox. O envio continua sendo
responsabilidade do worker de notificacoes, o que mantem retentativas e
observabilidade em um unico lugar.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, joinedload

from audit_service import record_event
from business_time import add_business_hours
from auth import ROLE_ADMIN
from config import settings
from database import (
    NotificationOutbox,
    SessionLocal,
    Ticket,
    TicketStatus,
    User,
    utcnow,
)
from outbox_service import (
    EVENT_SLA_OVERDUE,
    EVENT_SLA_WARNING,
    enqueue_ticket_update,
)

logger = logging.getLogger("geodemandas.sla_monitor")

_IGNORED_STATUSES = {
    TicketStatus.RESOLVIDO,
    TicketStatus.CONCLUIDO,
    TicketStatus.CANCELADO,
}
_MILESTONE_LABELS = {
    "first_response": "primeira resposta",
    "resolution": "resolucao",
}

_running = False
_stop_event: asyncio.Event | None = None
_worker_event_loop: asyncio.AbstractEventLoop | None = None


def _utc_naive(value: datetime) -> datetime:
    """Normaliza datas para o UTC sem timezone usado pelas colunas legadas."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _default_risk_window_minutes() -> int:
    configured_minutes = getattr(settings, "SLA_RISK_WINDOW_MINUTES", None)
    if configured_minutes is not None:
        return max(0, int(configured_minutes))
    configured_hours = getattr(settings, "SLA_RISK_WINDOW_HOURS", 4)
    return max(0, int(configured_hours) * 60)


def _default_interval_seconds() -> int:
    """Aceita nomes de configuracao novos sem exigir a migracao imediatamente."""
    for name in (
        "SLA_ALERT_POLL_INTERVAL_SECONDS",
        "SLA_ALERT_POLL_INTERVAL",
        "SLA_MONITOR_INTERVAL_SECONDS",
    ):
        value = getattr(settings, name, None)
        if value is not None:
            return max(1, int(value))
    return 300


def _ticket_milestones(ticket: Ticket) -> tuple[tuple[str, datetime], ...]:
    milestones: list[tuple[str, datetime]] = []
    if (
        ticket.first_response_at is None
        and ticket.first_response_due_at is not None
    ):
        milestones.append(
            ("first_response", _utc_naive(ticket.first_response_due_at))
        )
    if ticket.resolution_due_at is not None:
        milestones.append(("resolution", _utc_naive(ticket.resolution_due_at)))
    return tuple(milestones)


def _alert_level(
    due_at: datetime,
    *,
    now: datetime,
    risk_limit: datetime,
) -> str | None:
    # A fronteira exata do prazo ja e vencimento, nao apenas risco.
    if due_at <= now:
        return "overdue"
    if due_at <= risk_limit:
        return "risk"
    return None


def _recipients(ticket: Ticket, active_admins: tuple[User, ...]) -> tuple[User, ...]:
    """Prioriza o responsavel ativo e nunca devolve o solicitante."""
    assignee = ticket.assignee
    if (
        assignee is not None
        and assignee.is_active
        and assignee.id != ticket.requester_id
    ):
        return (assignee,)

    return tuple(
        admin for admin in active_admins if admin.id != ticket.requester_id
    )


def _dedupe_key(
    ticket_id: int,
    milestone: str,
    level: str,
    due_at: datetime,
    recipient_id: int,
) -> str:
    due_key = _utc_naive(due_at).isoformat(timespec="microseconds")
    return (
        f"sla:{ticket_id}:{milestone}:{level}:{due_key}:"
        f"recipient-{recipient_id}"
    )


def _generic_update_text(ticket_id: int, milestone: str, level: str) -> str:
    milestone_label = _MILESTONE_LABELS[milestone]
    if level == "overdue":
        condition = "ultrapassou"
    else:
        condition = "esta proxima de"
    return (
        f"A demanda #{ticket_id} {condition} o prazo de {milestone_label}. "
        "Acesse o GeoDemandas para revisar a fila."
    )


def process_sla_alerts(
    session_factory: Callable[[], Session] = SessionLocal,
    now: datetime | None = None,
    risk_window_minutes: int | None = None,
) -> dict[str, int]:
    """Enfileira alertas de SLA idempotentes e confirma tudo em um unico commit.

    ``risk`` e ``overdue`` contam somente notificacoes novas. Uma nova
    execucao para o mesmo marco, prazo e destinatario incrementa
    ``deduplicated`` e nao cria outro item.
    """
    current = _utc_naive(now or utcnow())
    window_minutes = (
        _default_risk_window_minutes()
        if risk_window_minutes is None
        else max(0, int(risk_window_minutes))
    )
    risk_limit = add_business_hours(current, window_minutes / 60)
    counts = {
        "tickets_scanned": 0,
        "queued": 0,
        "deduplicated": 0,
        "risk": 0,
        "overdue": 0,
        "without_recipient": 0,
    }

    db = session_factory()
    try:
        active_admins = tuple(
            db.query(User)
            .filter(
                User.role == ROLE_ADMIN,
                User.is_active.is_(True),
            )
            .order_by(User.id.asc())
            .all()
        )
        tickets = (
            db.query(Ticket)
            .options(joinedload(Ticket.assignee))
            .filter(
                Ticket.status.notin_(_IGNORED_STATUSES),
                Ticket.sla_paused_at.is_(None),
            )
            .order_by(Ticket.id.asc())
            .all()
        )
        counts["tickets_scanned"] = len(tickets)

        for ticket in tickets:
            recipients = _recipients(ticket, active_admins)
            for milestone, due_at in _ticket_milestones(ticket):
                level = _alert_level(
                    due_at,
                    now=current,
                    risk_limit=risk_limit,
                )
                if level is None:
                    continue
                if not recipients:
                    counts["without_recipient"] += 1
                    continue

                for recipient in recipients:
                    key = _dedupe_key(
                        ticket.id,
                        milestone,
                        level,
                        due_at,
                        recipient.id,
                    )
                    existing_id = (
                        db.query(NotificationOutbox.id)
                        .filter(NotificationOutbox.dedupe_key == key)
                        .scalar()
                    )
                    enqueue_ticket_update(
                        db,
                        recipient.email,
                        "Equipe de atendimento",
                        ticket.id,
                        "Alerta operacional de SLA",
                        _generic_update_text(ticket.id, milestone, level),
                        dedupe_key=key,
                        event_type=(
                            EVENT_SLA_OVERDUE
                            if level == "overdue"
                            else EVENT_SLA_WARNING
                        ),
                    )
                    if existing_id is not None:
                        counts["deduplicated"] += 1
                    else:
                        counts["queued"] += 1
                        counts[level] += 1
                        record_event(
                            db,
                            "ticket.sla.alert_queued",
                            ticket_id=ticket.id,
                            summary=(
                                "Alerta de SLA vencido enfileirado."
                                if level == "overdue"
                                else "Alerta preventivo de SLA enfileirado."
                            ),
                            changes={
                                "event_type": (
                                    EVENT_SLA_OVERDUE
                                    if level == "overdue"
                                    else EVENT_SLA_WARNING
                                ),
                                "status": level,
                                "due_at": due_at,
                            },
                            source="scheduler",
                            category="system",
                            actor_type="worker",
                        )

        # A criacao de todos os alertas pertence a uma unica transacao.
        db.commit()
        return counts
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def sla_alert_worker_loop(
    *,
    interval_seconds: int | float | None = None,
    risk_window_minutes: int | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    """Executa o monitor continuamente e permite interrupcao sem espera longa."""
    global _running, _stop_event, _worker_event_loop

    interval = (
        _default_interval_seconds()
        if interval_seconds is None
        else max(0.01, float(interval_seconds))
    )
    _running = True
    _worker_event_loop = asyncio.get_running_loop()
    _stop_event = asyncio.Event()
    logger.info(
        "Monitor de SLA iniciado (intervalo=%ss, janela=%smin)",
        interval,
        (
            _default_risk_window_minutes()
            if risk_window_minutes is None
            else max(0, int(risk_window_minutes))
        ),
    )

    try:
        while _running:
            try:
                counts = await asyncio.to_thread(
                    process_sla_alerts,
                    session_factory,
                    None,
                    risk_window_minutes,
                )
                if counts["queued"]:
                    logger.info(
                        "Alertas de SLA enfileirados: %s",
                        counts["queued"],
                    )
            except Exception:  # noqa: BLE001 - o proximo ciclo deve continuar
                logger.exception("Falha inesperada no ciclo do monitor de SLA")

            if not _running:
                break
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue
            break
    finally:
        _running = False
        _stop_event = None
        _worker_event_loop = None
        logger.info("Monitor de SLA encerrado")


def stop_sla_alert_worker() -> None:
    """Solicita o encerramento e acorda imediatamente um loop em espera."""
    global _running
    _running = False
    stop_event = _stop_event
    event_loop = _worker_event_loop
    if stop_event is None:
        return
    if event_loop is not None and event_loop.is_running():
        event_loop.call_soon_threadsafe(stop_event.set)
    else:
        stop_event.set()


# Nomes explicitos para facilitar a integracao no lifespan da aplicacao.
sla_monitor_worker_loop = sla_alert_worker_loop
stop_sla_monitor_worker = stop_sla_alert_worker
