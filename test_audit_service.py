"""Testes do registro transacional e da proteção de dados da auditoria."""
from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from audit_service import (
    AuditPayloadRejected,
    record_event,
    serialize_changes,
)
from database import AuditEvent, Base, TicketPriority, TicketStatus, User


class AuditServiceTests(unittest.TestCase):
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

    def test_event_participates_in_callers_transaction_and_rollback(self) -> None:
        db = self.Session()
        try:
            event = record_event(
                db,
                "ticket.created",
                ticket_id=41,
                summary="Demanda criada.",
                changes={"status": TicketStatus.ABERTO},
            )

            self.assertIn(event, db.new)
            self.assertIsNone(event.id)
            self.assertEqual(db.query(AuditEvent).count(), 0)
            db.flush()
            self.assertEqual(db.query(AuditEvent).count(), 1)
            db.rollback()
            self.assertEqual(db.query(AuditEvent).count(), 0)

            persisted = record_event(
                db,
                "ticket.created",
                ticket_id=42,
                summary="Demanda criada.",
                changes={"status": TicketStatus.ABERTO},
            )
            db.commit()

            stored = db.query(AuditEvent).one()
            self.assertEqual(stored.event_uuid, persisted.event_uuid)
            self.assertEqual(stored.ticket_id, 42)
        finally:
            db.close()

    def test_actor_is_snapshotted_without_email_fallback(self) -> None:
        db = self.Session()
        try:
            actor = User(
                id=17,
                email="maria.souza@example.com",
                full_name="Maria Souza",
                role="tecnico",
            )
            event = record_event(
                db,
                "ticket.status_changed",
                actor=actor,
                ticket_id=10,
                summary="Status da demanda alterado.",
                changes={
                    "status": {
                        "before": TicketStatus.ABERTO,
                        "after": TicketStatus.EM_TRIAGEM,
                    }
                },
            )

            self.assertEqual(event.actor_id, 17)
            self.assertEqual(event.actor_name, "Maria Souza")
            self.assertEqual(event.actor_role, "tecnico")
            self.assertEqual(event.actor_type, "user")
            self.assertNotIn(actor.email, event.actor_name)
        finally:
            db.rollback()
            db.close()

    def test_changes_json_is_deterministic_and_serializes_enum_and_datetime(self) -> None:
        due_at = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)
        first = serialize_changes(
            {
                "resolution_due_at": due_at,
                "priority": TicketPriority.URGENTE,
                "status": {
                    "after": TicketStatus.EM_ANDAMENTO,
                    "before": TicketStatus.EM_TRIAGEM,
                },
                "attachment_count": 2,
            }
        )
        second = serialize_changes(
            {
                "attachment_count": 2,
                "status": {
                    "before": TicketStatus.EM_TRIAGEM,
                    "after": TicketStatus.EM_ANDAMENTO,
                },
                "priority": TicketPriority.URGENTE,
                "resolution_due_at": due_at,
            }
        )

        self.assertEqual(first, second)
        decoded = json.loads(first)
        self.assertEqual(decoded["priority"], "Urgente")
        self.assertEqual(decoded["status"]["before"], "Em Triagem")
        self.assertEqual(
            decoded["resolution_due_at"],
            "2026-07-29T15:30:00+00:00",
        )

    def test_rejects_content_fields_recursively_before_adding_event(self) -> None:
        db = self.Session()
        try:
            forbidden_payloads = [
                {"content": "texto de comentário"},
                {"before": {"description": "descrição privada"}},
                {"status": {"after": {"note": "nota interna"}}},
                {"recipient_email": "requester@example.com"},
                {"raw_response": {"status": 200}},
            ]
            for changes in forbidden_payloads:
                with self.subTest(changes=changes):
                    with self.assertRaises(AuditPayloadRejected):
                        record_event(
                            db,
                            "ticket.updated",
                            ticket_id=7,
                            summary="Demanda atualizada.",
                            changes=changes,
                        )
            self.assertEqual(len(db.new), 0)
            self.assertEqual(db.query(AuditEvent).count(), 0)
        finally:
            db.rollback()
            db.close()

    def test_rejects_pii_or_credentials_even_under_allowed_keys(self) -> None:
        sensitive_values = [
            "pessoa@example.com",
            "Bearer eyJhbGciOiJIUzI1NiJ9.secret",
            "token=do-not-store",
            "192.168.10.24",
            "Mozilla/5.0",
        ]

        for value in sensitive_values:
            with self.subTest(value=value):
                with self.assertRaises(AuditPayloadRejected):
                    serialize_changes({"hub": value})

    def test_summary_is_generic_single_line_and_limited_to_column(self) -> None:
        db = self.Session()
        try:
            event = record_event(
                db,
                "ticket.updated",
                ticket_id=9,
                summary=("  Demanda   atualizada.\n" + ("x" * 300)),
                changes={"has_note": True, "attachment_count": 3},
            )
            self.assertNotIn("\n", event.summary)
            self.assertLessEqual(len(event.summary), 255)
            self.assertTrue(event.summary.endswith("…"))

            with self.assertRaises(AuditPayloadRejected):
                record_event(
                    db,
                    "ticket.updated",
                    ticket_id=9,
                    summary="Atualizado por pessoa@example.com",
                )
        finally:
            db.rollback()
            db.close()

    def test_persisted_events_are_append_only_through_the_orm(self) -> None:
        db = self.Session()
        try:
            event = record_event(
                db,
                "ticket.created",
                ticket_id=51,
                summary="Demanda criada.",
                changes={"status": TicketStatus.ABERTO},
            )
            db.commit()

            event.summary = "Resumo alterado."
            with self.assertRaisesRegex(ValueError, "nao podem ser alterados"):
                db.commit()
            db.rollback()

            persisted = db.get(AuditEvent, event.id)
            db.delete(persisted)
            with self.assertRaisesRegex(ValueError, "nao podem ser excluidos"):
                db.commit()
        finally:
            db.rollback()
            db.close()


if __name__ == "__main__":
    unittest.main()
