"""Testes isolados da outbox; nenhum transporte ou banco real e acessado."""
from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import (
    AuditEvent,
    Base,
    NotificationOutbox,
    Ticket,
    TicketPriority,
    TicketStatus,
    User,
    utcnow,
)
import database as database_module
from outbox_service import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    claim_notification_batch,
    enqueue_notification,
    enqueue_ticket_received,
    process_outbox_batch,
)


class NotificationOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _enqueue(
        self,
        *,
        event_type: str = "ticket_update",
        max_attempts: int = 3,
        dedupe_key: str | None = None,
    ) -> int:
        db = self.Session()
        try:
            item = enqueue_notification(
                db,
                event_type,
                "requester@example.com",
                {
                    "recipient_name": "Requester",
                    "ticket_id": 42,
                    "subject": "Test ticket",
                    "update_text": "Test update",
                },
                max_attempts=max_attempts,
                dedupe_key=dedupe_key,
            )
            db.commit()
            return item.id
        finally:
            db.close()

    def _get(self, item_id: int) -> NotificationOutbox:
        db = self.Session()
        try:
            return db.query(NotificationOutbox).filter_by(id=item_id).one()
        finally:
            db.close()

    def test_success_marks_item_sent(self) -> None:
        item_id = self._enqueue()
        processed = process_outbox_batch(
            session_factory=self.Session,
            dispatcher=lambda _item: True,
            now=utcnow() + timedelta(seconds=1),
        )

        item = self._get(item_id)
        self.assertEqual(processed, 1)
        self.assertEqual(item.status, STATUS_SENT)
        self.assertEqual(item.attempts, 1)
        self.assertIsNotNone(item.sent_at)
        self.assertIsNone(item.locked_at)
        self.assertIsNone(item.lock_token)
        self.assertIsNone(item.last_error)

    def test_transport_runs_without_an_open_database_transaction(self) -> None:
        self._enqueue()
        db = self.Session()
        transaction_states: list[bool] = []

        def dispatcher(_item: NotificationOutbox) -> bool:
            transaction_states.append(db.in_transaction())
            return True

        process_outbox_batch(
            session_factory=lambda: db,
            dispatcher=dispatcher,
            now=utcnow() + timedelta(seconds=1),
        )

        self.assertEqual(transaction_states, [False])

    def test_transient_failure_retries_then_succeeds(self) -> None:
        item_id = self._enqueue()
        first_now = utcnow() + timedelta(seconds=1)

        def temporary_failure(_item: NotificationOutbox) -> bool:
            raise RuntimeError(
                "SMTP requester@example.com token=do-not-persist"
            )

        process_outbox_batch(
            session_factory=self.Session,
            dispatcher=temporary_failure,
            now=first_now,
        )
        failed_once = self._get(item_id)
        self.assertEqual(failed_once.status, STATUS_PENDING)
        self.assertEqual(failed_once.attempts, 1)
        self.assertGreater(failed_once.next_attempt_at, first_now)
        self.assertNotIn("requester@example.com", failed_once.last_error)
        self.assertNotIn("do-not-persist", failed_once.last_error)

        process_outbox_batch(
            session_factory=self.Session,
            dispatcher=lambda _item: True,
            now=failed_once.next_attempt_at,
        )
        sent = self._get(item_id)
        self.assertEqual(sent.status, STATUS_SENT)
        self.assertEqual(sent.attempts, 2)

    def test_retry_limit_marks_item_failed(self) -> None:
        item_id = self._enqueue(max_attempts=2)
        first_now = utcnow() + timedelta(seconds=1)
        process_outbox_batch(
            session_factory=self.Session,
            dispatcher=lambda _item: False,
            now=first_now,
        )
        first = self._get(item_id)
        self.assertEqual(first.status, STATUS_PENDING)

        process_outbox_batch(
            session_factory=self.Session,
            dispatcher=lambda _item: False,
            now=first.next_attempt_at,
        )
        final = self._get(item_id)
        self.assertEqual(final.status, STATUS_FAILED)
        self.assertEqual(final.attempts, 2)
        self.assertIsNone(final.sent_at)
        db = self.Session()
        try:
            audit = (
                db.query(AuditEvent)
                .filter(AuditEvent.action == "notification.delivery_failed")
                .one()
            )
            self.assertEqual(audit.resource_id, str(item_id))
            self.assertNotIn("requester@example.com", audit.changes_json)
        finally:
            db.close()

    def test_permanent_payload_failure_does_not_retry(self) -> None:
        item_id = self._enqueue(event_type="unknown_event", max_attempts=5)
        process_outbox_batch(
            session_factory=self.Session,
            now=utcnow() + timedelta(seconds=1),
        )

        item = self._get(item_id)
        self.assertEqual(item.status, STATUS_FAILED)
        self.assertEqual(item.attempts, 1)
        self.assertIn("PermanentNotificationError", item.last_error)

    def test_stale_claim_is_recovered_after_worker_crash(self) -> None:
        item_id = self._enqueue()
        db = self.Session()
        item = db.get(NotificationOutbox, item_id)
        item.locked_at = utcnow() - timedelta(days=1)
        item.lock_token = "dead-worker"
        db.commit()
        db.close()

        process_outbox_batch(
            session_factory=self.Session,
            dispatcher=lambda _item: True,
            now=utcnow() + timedelta(seconds=1),
        )
        recovered = self._get(item_id)
        self.assertEqual(recovered.status, STATUS_SENT)
        self.assertIsNone(recovered.locked_at)
        self.assertIsNone(recovered.lock_token)

    def test_business_record_and_outbox_share_atomic_transaction(self) -> None:
        db = self.Session()
        requester = User(
            email="atomic@example.com",
            full_name="Atomic User",
            role="solicitante",
        )
        db.add(requester)
        db.commit()

        ticket = Ticket(
            subject="Atomic ticket",
            body="The ticket and its notification must commit together.",
            status=TicketStatus.ABERTO,
            priority=TicketPriority.MEDIA,
            requester_id=requester.id,
        )
        db.add(ticket)
        db.flush()
        enqueue_ticket_received(
            db,
            requester.email,
            requester.full_name,
            ticket.id,
            ticket.subject,
            ticket.priority.value,
            dedupe_key=f"ticket-received:{ticket.id}",
        )
        db.rollback()
        self.assertEqual(db.query(Ticket).count(), 0)
        self.assertEqual(db.query(NotificationOutbox).count(), 0)

        ticket = Ticket(
            subject="Committed ticket",
            body="This transaction is expected to be persisted.",
            status=TicketStatus.ABERTO,
            priority=TicketPriority.MEDIA,
            requester_id=requester.id,
        )
        db.add(ticket)
        db.flush()
        enqueue_ticket_received(
            db,
            requester.email,
            requester.full_name,
            ticket.id,
            ticket.subject,
            ticket.priority.value,
            dedupe_key=f"ticket-received:{ticket.id}",
        )
        db.commit()
        item = db.query(NotificationOutbox).one()
        self.assertEqual(item.ticket_id, ticket.id)
        db.close()

    def test_dedupe_and_active_lock_prevent_duplicate_claim(self) -> None:
        db = self.Session()
        first = enqueue_notification(
            db,
            "ticket_update",
            "requester@example.com",
            {"value": "snapshot"},
            dedupe_key="sla-risk:7:2030-01-01T00:00:00",
        )
        second = enqueue_notification(
            db,
            "ticket_update",
            "requester@example.com",
            {"value": "new snapshot"},
            dedupe_key="sla-risk:7:2030-01-01T00:00:00",
        )
        db.commit()
        self.assertEqual(first.id, second.id)
        self.assertEqual(db.query(NotificationOutbox).count(), 1)

        now = utcnow() + timedelta(seconds=1)
        claimed = claim_notification_batch(db, now=now)
        self.assertEqual([item.id for item in claimed], [first.id])
        other_db = self.Session()
        try:
            self.assertEqual(
                claim_notification_batch(other_db, now=now),
                [],
            )
        finally:
            other_db.close()
            db.close()

    def test_lightweight_migration_adds_columns_and_queue_indexes(self) -> None:
        legacy_engine = create_engine("sqlite://")
        with legacy_engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE tickets (id INTEGER PRIMARY KEY)")
            )
            connection.execute(
                text(
                    "CREATE TABLE notification_outbox "
                    "(id INTEGER PRIMARY KEY)"
                )
            )

        with patch.object(database_module, "engine", legacy_engine):
            database_module._ensure_notification_outbox_schema()

        schema = inspect(legacy_engine)
        columns = {
            column["name"]
            for column in schema.get_columns("notification_outbox")
        }
        indexes = {
            index["name"]
            for index in schema.get_indexes("notification_outbox")
        }
        self.assertIn("ticket_id", columns)
        self.assertIn("dedupe_key", columns)
        self.assertIn("next_attempt_at", columns)
        self.assertIn(
            "ix_notification_outbox_status_next_attempt_at",
            indexes,
        )
        self.assertIn("ix_notification_outbox_ticket_id", indexes)
        self.assertIn("ux_notification_outbox_dedupe_key", indexes)
        legacy_engine.dispose()


if __name__ == "__main__":
    unittest.main()
