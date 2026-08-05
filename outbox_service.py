"""Outbox transacional e worker duravel para notificacoes do GeoDemandas."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from audit_service import record_event
from config import settings
from database import NotificationOutbox, SessionLocal, utcnow
import notification_service
from operational_health import beat

logger = logging.getLogger("geodemandas.outbox")

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

EVENT_TICKET_RECEIVED = "ticket_received"
EVENT_TICKET_UPDATE = "ticket_update"
EVENT_TICKET_COMPLETED = "ticket_completed"
EVENT_SLA_WARNING = "sla_warning"
EVENT_SLA_OVERDUE = "sla_overdue"

_running = False
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SECRET_RE = re.compile(
    r"(?i)\b(password|secret|token|authorization|credential)"
    r"\s*[:=]\s*([^\s,;]+)"
)


class PermanentNotificationError(ValueError):
    """Erro de dados que uma nova tentativa nao consegue corrigir."""


class RetryableNotificationError(RuntimeError):
    """Falha temporaria do transporte de notificacao."""


def enqueue_notification(
    db: Session,
    event_type: str,
    recipient: str,
    payload: Mapping[str, Any],
    *,
    ticket_id: int | None = None,
    dedupe_key: str | None = None,
    max_attempts: int | None = None,
) -> NotificationOutbox:
    """Adiciona a notificacao a transacao atual sem executar ``commit``.

    Quando ``dedupe_key`` e informado, chamadas repetidas devolvem o mesmo
    registro. O savepoint trata tambem a corrida entre duas transacoes sem
    desfazer as alteracoes de negocio da transacao externa.
    """
    normalized_key = (dedupe_key or "").strip() or None
    if normalized_key and len(normalized_key) > 500:
        raise ValueError("dedupe_key excede 500 caracteres")

    if normalized_key:
        existing = (
            db.query(NotificationOutbox)
            .filter(NotificationOutbox.dedupe_key == normalized_key)
            .first()
        )
        if existing is not None:
            return existing

    item = NotificationOutbox(
        ticket_id=ticket_id,
        event_type=event_type.strip(),
        recipient=recipient.strip(),
        payload_json=json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        dedupe_key=normalized_key,
        status=STATUS_PENDING,
        attempts=0,
        max_attempts=max(
            1,
            max_attempts
            if max_attempts is not None
            else settings.NOTIFICATION_OUTBOX_MAX_ATTEMPTS,
        ),
        next_attempt_at=utcnow(),
    )

    if normalized_key is None:
        db.add(item)
        return item

    try:
        with db.begin_nested():
            db.add(item)
            db.flush([item])
    except IntegrityError:
        existing = (
            db.query(NotificationOutbox)
            .filter(NotificationOutbox.dedupe_key == normalized_key)
            .first()
        )
        if existing is None:
            raise
        return existing
    return item


def enqueue_ticket_received(
    db: Session,
    recipient: str,
    recipient_name: str,
    ticket_id: int,
    subject: str,
    priority: str,
    *,
    dedupe_key: str | None = None,
) -> NotificationOutbox:
    return enqueue_notification(
        db,
        EVENT_TICKET_RECEIVED,
        recipient,
        {
            "recipient_name": recipient_name,
            "ticket_id": ticket_id,
            "subject": subject,
            "priority": priority,
        },
        ticket_id=ticket_id,
        dedupe_key=dedupe_key,
    )


def enqueue_ticket_update(
    db: Session,
    recipient: str,
    recipient_name: str,
    ticket_id: int,
    subject: str,
    update_text: str,
    *,
    dedupe_key: str | None = None,
    event_type: str = EVENT_TICKET_UPDATE,
) -> NotificationOutbox:
    return enqueue_notification(
        db,
        event_type,
        recipient,
        {
            "recipient_name": recipient_name,
            "ticket_id": ticket_id,
            "subject": subject,
            "update_text": update_text,
        },
        ticket_id=ticket_id,
        dedupe_key=dedupe_key,
    )


def enqueue_ticket_completed(
    db: Session,
    recipient: str,
    recipient_name: str,
    ticket_id: int,
    subject: str,
    *,
    dedupe_key: str | None = None,
) -> NotificationOutbox:
    return enqueue_notification(
        db,
        EVENT_TICKET_COMPLETED,
        recipient,
        {
            "recipient_name": recipient_name,
            "ticket_id": ticket_id,
            "subject": subject,
        },
        ticket_id=ticket_id,
        dedupe_key=dedupe_key,
    )


def _required(payload: dict[str, Any], field: str, expected: type) -> Any:
    value = payload.get(field)
    if not isinstance(value, expected):
        raise PermanentNotificationError(
            f"campo obrigatorio invalido: {field}"
        )
    if expected is str and not value.strip():
        raise PermanentNotificationError(
            f"campo obrigatorio vazio: {field}"
        )
    return value


def dispatch_notification(item: NotificationOutbox) -> bool:
    """Converte um item persistido em uma chamada ao transporte existente."""
    recipient = (item.recipient or "").strip()
    if (
        not recipient
        or "@" not in recipient
        or len(recipient) > 255
        or any(char.isspace() for char in recipient)
    ):
        raise PermanentNotificationError("destinatario invalido")

    try:
        payload = json.loads(item.payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PermanentNotificationError("payload JSON invalido") from exc
    if not isinstance(payload, dict):
        raise PermanentNotificationError("payload deve ser um objeto")

    recipient_name = _required(payload, "recipient_name", str)
    ticket_id = _required(payload, "ticket_id", int)
    subject = _required(payload, "subject", str)

    if item.event_type == EVENT_TICKET_RECEIVED:
        priority = _required(payload, "priority", str)
        return notification_service.send_ticket_received(
            recipient,
            recipient_name,
            ticket_id,
            subject,
            priority,
        )
    if item.event_type in {
        EVENT_TICKET_UPDATE,
        EVENT_SLA_WARNING,
        EVENT_SLA_OVERDUE,
    }:
        update_text = _required(payload, "update_text", str)
        return notification_service.send_ticket_update(
            recipient,
            recipient_name,
            ticket_id,
            subject,
            update_text,
        )
    if item.event_type == EVENT_TICKET_COMPLETED:
        return notification_service.send_ticket_completed(
            recipient,
            recipient_name,
            ticket_id,
            subject,
        )
    raise PermanentNotificationError("tipo de notificacao desconhecido")


def _sanitize_error(exc: BaseException) -> str:
    """Retem diagnostico util sem persistir e-mail, token ou segredo."""
    message = " ".join(str(exc).split())
    message = _EMAIL_RE.sub("[email-redacted]", message)
    message = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", message)
    safe = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return safe[:500]


def recover_stuck_notifications(
    db: Session,
    *,
    now: datetime | None = None,
) -> int:
    """Libera itens cujo worker morreu depois do claim."""
    current = now or utcnow()
    cutoff = current - timedelta(
        seconds=max(1, settings.NOTIFICATION_OUTBOX_LOCK_TIMEOUT_SECONDS)
    )
    return (
        db.query(NotificationOutbox)
        .filter(
            NotificationOutbox.status == STATUS_PENDING,
            NotificationOutbox.locked_at.is_not(None),
            NotificationOutbox.locked_at <= cutoff,
        )
        .update(
            {
                NotificationOutbox.locked_at: None,
                NotificationOutbox.lock_token: None,
                NotificationOutbox.updated_at: current,
            },
            synchronize_session=False,
        )
    )


def claim_notification_batch(
    db: Session,
    *,
    now: datetime | None = None,
    batch_size: int | None = None,
) -> list[NotificationOutbox]:
    """Faz claim com compare-and-set, evitando envio por dois workers.

    O UPDATE condicional e atomico nos dialetos suportados. No SQLite os
    escritores sao serializados pelo proprio arquivo; ainda assim recomenda-se
    um unico processo de worker para reduzir contencao.
    """
    current = now or utcnow()
    size = max(
        1,
        batch_size
        if batch_size is not None
        else settings.NOTIFICATION_OUTBOX_BATCH_SIZE,
    )
    stale_before = current - timedelta(
        seconds=max(1, settings.NOTIFICATION_OUTBOX_LOCK_TIMEOUT_SECONDS)
    )
    eligible_lock = or_(
        NotificationOutbox.locked_at.is_(None),
        NotificationOutbox.locked_at <= stale_before,
    )
    candidate_ids = [
        item_id
        for (item_id,) in (
            db.query(NotificationOutbox.id)
            .filter(
                NotificationOutbox.status == STATUS_PENDING,
                or_(
                    NotificationOutbox.next_attempt_at.is_(None),
                    NotificationOutbox.next_attempt_at <= current,
                ),
                eligible_lock,
            )
            .order_by(
                NotificationOutbox.next_attempt_at.asc(),
                NotificationOutbox.id.asc(),
            )
            .limit(size * 4)
            .all()
        )
    ]

    token = uuid.uuid4().hex
    claimed = 0
    for item_id in candidate_ids:
        updated = (
            db.query(NotificationOutbox)
            .filter(
                NotificationOutbox.id == item_id,
                NotificationOutbox.status == STATUS_PENDING,
                or_(
                    NotificationOutbox.next_attempt_at.is_(None),
                    NotificationOutbox.next_attempt_at <= current,
                ),
                or_(
                    NotificationOutbox.locked_at.is_(None),
                    NotificationOutbox.locked_at <= stale_before,
                ),
            )
            .update(
                {
                    NotificationOutbox.locked_at: current,
                    NotificationOutbox.lock_token: token,
                    NotificationOutbox.updated_at: current,
                },
                synchronize_session=False,
            )
        )
        claimed += updated
        if claimed >= size:
            break
    db.commit()
    if not claimed:
        return []
    claimed_items = (
        db.query(NotificationOutbox)
        .filter(
            NotificationOutbox.status == STATUS_PENDING,
            NotificationOutbox.lock_token == token,
        )
        .order_by(NotificationOutbox.id.asc())
        .all()
    )
    # O transporte pode levar varios segundos. Devolvemos snapshots destacados
    # e encerramos a transacao de leitura antes de qualquer chamada externa.
    for item in claimed_items:
        db.expunge(item)
    db.rollback()
    return claimed_items


def _retry_delay_seconds(attempts: int) -> int:
    base = max(1, settings.NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS)
    ceiling = max(base, settings.NOTIFICATION_OUTBOX_RETRY_MAX_SECONDS)
    return min(ceiling, base * (2 ** max(0, attempts - 1)))


def process_outbox_batch(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    dispatcher: Callable[[NotificationOutbox], bool] | None = None,
    now: datetime | None = None,
    batch_size: int | None = None,
) -> int:
    """Processa um lote e confirma o resultado individualmente."""
    send = dispatcher or dispatch_notification
    current = now or utcnow()
    db = session_factory()
    try:
        recovered = recover_stuck_notifications(db, now=current)
        db.commit()
        if recovered:
            logger.warning("%s notificacao(oes) travada(s) recuperada(s)", recovered)

        items = claim_notification_batch(
            db,
            now=current,
            batch_size=batch_size,
        )
        for claimed_item in items:
            outcome = STATUS_SENT
            failure: BaseException | None = None
            try:
                if send(claimed_item) is not True:
                    raise RetryableNotificationError(
                        "transporte nao confirmou o envio"
                    )
            except PermanentNotificationError as exc:
                outcome = STATUS_FAILED
                failure = exc
            except Exception as exc:  # noqa: BLE001 - falha do transporte
                outcome = STATUS_PENDING
                failure = exc

            # Somente depois do transporte abrimos a transacao que confirma o
            # resultado. O token impede um worker sem a lease atual de gravar.
            item = (
                db.query(NotificationOutbox)
                .filter(
                    NotificationOutbox.id == claimed_item.id,
                    NotificationOutbox.status == STATUS_PENDING,
                    NotificationOutbox.lock_token == claimed_item.lock_token,
                )
                .first()
            )
            if item is None:
                db.rollback()
                continue

            item.attempts += 1
            if failure is not None:
                item.last_error = _sanitize_error(failure)
                if outcome == STATUS_FAILED:
                    item.status = STATUS_FAILED
                elif item.attempts >= max(1, item.max_attempts):
                    item.status = STATUS_FAILED
                else:
                    item.status = STATUS_PENDING
                    item.next_attempt_at = current + timedelta(
                        seconds=_retry_delay_seconds(item.attempts)
                    )
            else:
                item.status = STATUS_SENT
                item.sent_at = current
                item.last_error = None

            try:
                if item.status == STATUS_FAILED:
                    record_event(
                        db,
                        "notification.delivery_failed",
                        ticket_id=item.ticket_id,
                        summary=(
                            f"Entrega #{item.id} atingiu falha definitiva."
                        ),
                        changes={
                            "notification_id": item.id,
                            "event_type": item.event_type,
                            "status": item.status,
                            "attempts": item.attempts,
                            "max_attempts": item.max_attempts,
                        },
                        source="worker",
                        category="system",
                        actor_type="worker",
                        resource_type="notification",
                        resource_id=str(item.id),
                    )
                item.locked_at = None
                item.lock_token = None
                item.updated_at = current
                db.commit()
            except Exception:
                db.rollback()
                raise
        return len(items)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def notification_outbox_worker_loop() -> None:
    """Executa lotes continuamente sem bloquear o event loop do FastAPI."""
    global _running
    _running = True
    logger.info(
        "Worker da outbox iniciado (lote=%s, intervalo=%ss)",
        settings.NOTIFICATION_OUTBOX_BATCH_SIZE,
        settings.NOTIFICATION_OUTBOX_POLL_INTERVAL,
    )
    while _running:
        beat("outbox")
        try:
            await asyncio.to_thread(process_outbox_batch)
        except Exception:  # noqa: BLE001 - o proximo ciclo deve continuar
            logger.exception("Falha inesperada no ciclo da outbox")
        await asyncio.sleep(
            max(1, settings.NOTIFICATION_OUTBOX_POLL_INTERVAL)
        )


def stop_notification_outbox_worker() -> None:
    global _running
    _running = False
