"""Testes integrados de autorização, mensagens e workflow da API."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

with patch.dict(
    os.environ,
    {
        "DATABASE_URL": "sqlite:///:memory:",
        "DEV_MODE": "false",
    },
):
    from auth import get_current_user
    from database import (
        Attachment,
        AuditEvent,
        Base,
        Comment,
        NotificationOutbox,
        Ticket,
        TicketPriority,
        TicketStatus,
        User,
        get_db,
        utcnow,
    )
    from routes import api
    from workflow_service import initialize_sla


class ApiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        db = self.Session()
        requester = User(
            email="solicitante@example.com",
            full_name="Pessoa Solicitante",
            role="solicitante",
            is_active=True,
        )
        technician = User(
            email="tecnico@example.com",
            full_name="Pessoa Técnica",
            role="tecnico",
            is_technician=True,
            is_active=True,
        )
        legacy_user = User(
            email="legado@example.com",
            full_name="Perfil Legado",
            role="papel_invalido",
            is_active=True,
        )
        db.add_all([requester, technician, legacy_user])
        db.flush()
        ticket = Ticket(
            subject="Demanda de teste",
            body="Descrição da demanda",
            requester_id=requester.id,
            assignee_id=technician.id,
            priority=TicketPriority.MEDIA,
            status=TicketStatus.EM_ANDAMENTO,
            source_channel="portal",
            created_at=datetime(2026, 7, 29, 9, 0),
            updated_at=datetime(2026, 7, 29, 9, 0),
            last_activity_at=datetime(2026, 7, 29, 9, 0),
        )
        initialize_sla(ticket, now=ticket.created_at)
        db.add(ticket)
        db.flush()
        public_comment = Comment(
            ticket_id=ticket.id,
            author_id=technician.id,
            content="Resposta pública",
            is_internal=False,
        )
        internal_comment = Comment(
            ticket_id=ticket.id,
            author_id=technician.id,
            content="Diagnóstico reservado",
            is_internal=True,
        )
        db.add_all([public_comment, internal_comment])
        db.commit()
        self.requester_id = requester.id
        self.technician_id = technician.id
        self.legacy_user_id = legacy_user.id
        self.ticket_id = ticket.id
        self.public_comment_id = public_comment.id
        db.close()

        self.current_user_id = self.requester_id
        app = FastAPI()
        app.include_router(api.router)

        def override_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        def override_user():
            session = self.Session()
            try:
                user = session.get(User, self.current_user_id)
                session.expunge(user)
                return user
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _audit_event(self, action: str) -> AuditEvent:
        db = self.Session()
        try:
            event = (
                db.query(AuditEvent)
                .filter(AuditEvent.action == action)
                .order_by(AuditEvent.id.desc())
                .first()
            )
            self.assertIsNotNone(event, f"Evento de auditoria ausente: {action}")
            db.expunge(event)
            return event
        finally:
            db.close()

    def test_requester_state_hides_internal_notes(self) -> None:
        response = self.client.get(f"/api/tickets/{self.ticket_id}/state")

        self.assertEqual(response.status_code, 200)
        contents = [item["content"] for item in response.json()["comments"]]
        self.assertIn("Resposta pública", contents)
        self.assertNotIn("Diagnóstico reservado", contents)

    def test_staff_state_includes_internal_notes(self) -> None:
        self.current_user_id = self.technician_id

        response = self.client.get(f"/api/tickets/{self.ticket_id}/state")

        self.assertEqual(response.status_code, 200)
        comments = response.json()["comments"]
        self.assertTrue(
            any(
                item["content"] == "Diagnóstico reservado"
                and item["is_internal"] is True
                for item in comments
            )
        )

    def test_incremental_state_does_not_resend_history(self) -> None:
        response = self.client.get(
            f"/api/tickets/{self.ticket_id}/state",
            params={"after_comment_id": self.public_comment_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["comments"], [])

    def test_unknown_role_cannot_access_someone_elses_ticket(self) -> None:
        self.current_user_id = self.legacy_user_id

        response = self.client.get(f"/api/tickets/{self.ticket_id}/state")

        self.assertEqual(response.status_code, 404)

    def test_requester_cannot_create_internal_note(self) -> None:
        response = self.client.post(
            f"/api/tickets/{self.ticket_id}/comments",
            json={"content": "Tentativa", "is_internal": True},
        )

        self.assertEqual(response.status_code, 403)

    def test_blank_comment_is_rejected_after_trimming(self) -> None:
        response = self.client.post(
            f"/api/tickets/{self.ticket_id}/comments",
            json={"content": "   "},
        )

        self.assertEqual(response.status_code, 422)

    def test_requester_reply_returns_waiting_ticket_to_active_queue(self) -> None:
        db = self.Session()
        ticket = db.get(Ticket, self.ticket_id)
        ticket.status = TicketStatus.AGUARDANDO_SOLICITANTE
        ticket.sla_paused_at = utcnow() - timedelta(hours=2)
        db.commit()
        db.close()

        response = self.client.post(
            f"/api/tickets/{self.ticket_id}/comments",
            json={"content": "Envio as informações solicitadas."},
        )

        self.assertEqual(response.status_code, 200)
        db = self.Session()
        ticket = db.get(Ticket, self.ticket_id)
        self.assertEqual(ticket.status, TicketStatus.EM_ANDAMENTO)
        self.assertIsNone(ticket.sla_paused_at)
        self.assertTrue(
            db.query(Comment)
            .filter(
                Comment.ticket_id == self.ticket_id,
                Comment.is_system.is_(True),
                Comment.content.contains("Resposta do solicitante"),
            )
            .count()
        )
        queued = db.query(NotificationOutbox).one()
        self.assertNotIn(
            "Envio as informações solicitadas.",
            queued.payload_json,
        )
        self.assertNotIn("Pessoa Solicitante", queued.payload_json)
        public_audit = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "ticket.comment.public_added")
            .one()
        )
        self.assertEqual(
            json.loads(public_audit.changes_json),
            {
                "attachment_count": 0,
                "comment_id": response.json()["id"],
                "has_note": True,
            },
        )
        self.assertNotIn(response.json()["content"], public_audit.changes_json)
        automatic_audit = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "ticket.status.auto_changed")
            .one()
        )
        self.assertEqual(
            json.loads(automatic_audit.changes_json),
            {
                "after": {"status": TicketStatus.EM_ANDAMENTO.value},
                "before": {
                    "status": TicketStatus.AGUARDANDO_SOLICITANTE.value,
                },
            },
        )
        db.close()

    def test_public_staff_status_update_counts_as_first_response(self) -> None:
        self.current_user_id = self.technician_id

        response = self.client.patch(
            f"/api/tickets/{self.ticket_id}/status",
            json={"status": TicketStatus.AGUARDANDO_SOLICITANTE.value},
        )

        self.assertEqual(response.status_code, 200)
        db = self.Session()
        ticket = db.get(Ticket, self.ticket_id)
        self.assertEqual(ticket.status, TicketStatus.AGUARDANDO_SOLICITANTE)
        self.assertIsNotNone(ticket.first_response_at)
        self.assertIsNotNone(ticket.sla_paused_at)
        audit = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "ticket.status.changed")
            .one()
        )
        changes = json.loads(audit.changes_json)
        self.assertEqual(
            changes["before"],
            {"status": TicketStatus.EM_ANDAMENTO.value},
        )
        self.assertEqual(
            changes["after"],
            {"status": TicketStatus.AGUARDANDO_SOLICITANTE.value},
        )
        self.assertFalse(changes["has_note"])
        self.assertEqual(changes["comment_id"], response.json()["id"])
        db.close()

    def test_status_note_is_not_persisted_in_notification_or_audit(self) -> None:
        self.current_user_id = self.technician_id
        sensitive_note = "STATUS_NOTE_SENTINEL_93"

        response = self.client.patch(
            f"/api/tickets/{self.ticket_id}/status",
            json={
                "status": TicketStatus.BLOQUEADO.value,
                "note": sensitive_note,
            },
        )

        self.assertEqual(response.status_code, 200)
        db = self.Session()
        queued = db.query(NotificationOutbox).one()
        audit = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "ticket.status.changed")
            .one()
        )
        self.assertNotIn(sensitive_note, queued.payload_json)
        self.assertNotIn(sensitive_note, audit.changes_json)
        self.assertNotIn(sensitive_note, audit.summary)
        self.assertIn(TicketStatus.BLOQUEADO.value, queued.payload_json)
        db.close()

    def test_internal_message_is_audited_without_notification(self) -> None:
        self.current_user_id = self.technician_id
        secret_marker = "INTERNAL_SENTINEL_42"

        response = self.client.post(
            f"/api/tickets/{self.ticket_id}/messages",
            data={"content": secret_marker, "is_internal": "true"},
        )

        self.assertEqual(response.status_code, 200)
        db = self.Session()
        self.assertEqual(db.query(NotificationOutbox).count(), 0)
        audit = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "ticket.comment.internal_added")
            .one()
        )
        self.assertEqual(
            json.loads(audit.changes_json),
            {
                "attachment_count": 0,
                "comment_id": response.json()["id"],
                "has_note": True,
            },
        )
        self.assertNotIn(secret_marker, audit.changes_json)
        self.assertNotIn(secret_marker, audit.summary)
        db.close()

    def test_assignee_change_audits_only_ids_and_comment_id(self) -> None:
        self.current_user_id = self.technician_id

        response = self.client.patch(
            f"/api/tickets/{self.ticket_id}/assignee",
            json={"assignee_id": None},
        )

        self.assertEqual(response.status_code, 200)
        audit = self._audit_event("ticket.assignee.changed")
        self.assertEqual(
            json.loads(audit.changes_json),
            {
                "after": {"assignee_id": None},
                "before": {"assignee_id": self.technician_id},
                "comment_id": response.json()["id"],
            },
        )
        self.assertNotIn("Pessoa", audit.changes_json)

    def test_metadata_change_audits_only_structured_fields(self) -> None:
        self.current_user_id = self.technician_id

        response = self.client.patch(
            f"/api/tickets/{self.ticket_id}/metadata",
            json={
                "priority": TicketPriority.ALTA.value,
                "category": "Infra",
                "hub": "BH",
            },
        )

        self.assertEqual(response.status_code, 200)
        audit = self._audit_event("ticket.metadata.changed")
        self.assertEqual(
            json.loads(audit.changes_json),
            {
                "after": {
                    "category": "Infra",
                    "hub": "BH",
                    "priority": TicketPriority.ALTA.value,
                },
                "before": {
                    "category": None,
                    "hub": None,
                    "priority": TicketPriority.MEDIA.value,
                },
                "comment_id": response.json()["id"],
            },
        )
        self.assertNotIn("example.com", audit.changes_json)

    def test_attachment_preview_is_not_audited_but_explicit_download_is(self) -> None:
        db = self.Session()
        attachment = Attachment(
            ticket_id=self.ticket_id,
            uploader_id=self.requester_id,
            original_name="evidence.pdf",
            stored_name="test/evidence.pdf",
            content_type="application/pdf",
            size_bytes=3,
        )
        db.add(attachment)
        db.commit()
        attachment_id = attachment.id
        db.close()

        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as handle:
                handle.write(b"pdf")
                temporary_path = handle.name
            with patch.object(
                api,
                "attachment_path",
                return_value=Path(temporary_path),
            ):
                preview = self.client.get(f"/api/attachments/{attachment_id}")
                download = self.client.get(
                    f"/api/attachments/{attachment_id}",
                    params={"download": "true"},
                )

            self.assertEqual(preview.status_code, 200)
            self.assertEqual(
                preview.headers["content-disposition"].split(";", 1)[0],
                "inline",
            )
            self.assertEqual(download.status_code, 200)
            self.assertEqual(
                download.headers["content-disposition"].split(";", 1)[0],
                "attachment",
            )
            db = self.Session()
            events = (
                db.query(AuditEvent)
                .filter(AuditEvent.action == "ticket.attachment.downloaded")
                .all()
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(
                json.loads(events[0].changes_json),
                {"attachment_id": attachment_id},
            )
            db.close()
        finally:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
