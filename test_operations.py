"""Testes integrados do painel administrativo de operações."""
from __future__ import annotations

import os
import re
import unittest
from datetime import timedelta
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
    from business_time import add_business_hours
    from database import (
        AuditEvent,
        Base,
        NotificationOutbox,
        Ticket,
        TicketPriority,
        TicketStatus,
        User,
        get_db,
        utcnow,
    )
    from routes import operations


class OperationsRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)

        db = self.Session()
        admin = User(
            email="admin@example.com",
            full_name="Pessoa Administradora",
            role="administrador",
            is_active=True,
        )
        requester = User(
            email="requester@example.com",
            full_name="Pessoa Solicitante",
            role="solicitante",
            is_active=True,
        )
        technician = User(
            email="technician@example.com",
            full_name="Pessoa Tecnica",
            role="tecnico",
            is_technician=True,
            is_active=True,
        )
        db.add_all([admin, requester, technician])
        db.flush()

        now = utcnow()
        risk_ticket = self._ticket(
            requester_id=requester.id,
            assignee_id=None,
            subject="Demanda em risco",
            due_at=add_business_hours(now, 1),
        )
        overdue_ticket = self._ticket(
            requester_id=requester.id,
            assignee_id=technician.id,
            subject="Demanda vencida",
            due_at=now - timedelta(hours=1),
        )
        healthy_ticket = self._ticket(
            requester_id=requester.id,
            assignee_id=None,
            subject="Demanda dentro do prazo",
            due_at=add_business_hours(now, 12),
        )
        db.add_all([risk_ticket, overdue_ticket, healthy_ticket])
        db.flush()

        pending_received = self._notification(
            ticket_id=risk_ticket.id,
            event_type="ticket_received",
            recipient="pending@example.com",
            status="pending",
            created_at=now - timedelta(minutes=4),
        )
        pending_sla = self._notification(
            ticket_id=risk_ticket.id,
            event_type="sla_warning",
            recipient="sla@example.com",
            status="pending",
            created_at=now - timedelta(minutes=3),
        )
        failed = self._notification(
            ticket_id=overdue_ticket.id,
            event_type="ticket_update",
            recipient="failure@example.com",
            status="failed",
            attempts=5,
            last_error="Falha temporaria do transporte",
            locked_at=now - timedelta(minutes=1),
            lock_token="worker-token",
            created_at=now - timedelta(minutes=2),
        )
        sent = self._notification(
            ticket_id=healthy_ticket.id,
            event_type="ticket_completed",
            recipient="sent@example.com",
            status="sent",
            attempts=1,
            sent_at=now - timedelta(minutes=1),
            created_at=now - timedelta(minutes=1),
        )
        db.add_all([pending_received, pending_sla, failed, sent])
        db.commit()

        self.admin_id = admin.id
        self.requester_id = requester.id
        self.current_user_id = self.admin_id
        self.failed_id = failed.id
        self.sent_id = sent.id
        self.initial_delivery_count = 4
        db.close()

        app = FastAPI()
        app.include_router(operations.router)

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

    @staticmethod
    def _ticket(
        *,
        requester_id: int,
        assignee_id: int | None,
        subject: str,
        due_at,
    ) -> Ticket:
        now = utcnow()
        return Ticket(
            subject=subject,
            body="Descricao operacional",
            requester_id=requester_id,
            assignee_id=assignee_id,
            priority=TicketPriority.MEDIA,
            status=TicketStatus.ABERTO,
            source_channel="portal",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
            first_response_due_at=due_at,
            resolution_due_at=due_at + timedelta(hours=24),
        )

    @staticmethod
    def _notification(
        *,
        ticket_id: int | None,
        event_type: str,
        recipient: str,
        status: str,
        attempts: int = 0,
        last_error: str | None = None,
        locked_at=None,
        lock_token: str | None = None,
        sent_at=None,
        created_at=None,
    ) -> NotificationOutbox:
        created_at = created_at or utcnow()
        return NotificationOutbox(
            ticket_id=ticket_id,
            event_type=event_type,
            recipient=recipient,
            payload_json='{"ticket_id": 1}',
            status=status,
            attempts=attempts,
            max_attempts=5,
            next_attempt_at=created_at,
            locked_at=locked_at,
            lock_token=lock_token,
            sent_at=sent_at,
            last_error=last_error,
            created_at=created_at,
            updated_at=created_at,
        )

    def assert_metric(self, html: str, label: str, value: int) -> None:
        pattern = (
            rf"{re.escape(label)}</p>\s*"
            rf'<p[^>]*>\s*{value}\s*</p>'
        )
        self.assertRegex(html, pattern)

    def test_admin_sees_rendered_operations_dashboard(self) -> None:
        response = self.client.get("/admin/operacao")

        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>Operações e confiabilidade", response.text)
        self.assertIn("<h1", response.text)
        self.assertIn("Operações e confiabilidade</h1>", response.text)
        self.assertIn("Pessoa Administradora", response.text)
        self.assertIn("Fila de entregas", response.text)

    def test_requester_receives_forbidden(self) -> None:
        self.current_user_id = self.requester_id

        response = self.client.get("/admin/operacao")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "Você não tem permissão para realizar esta ação.",
        )

    def test_dashboard_reports_delivery_and_ticket_metrics(self) -> None:
        response = self.client.get("/admin/operacao")

        self.assertEqual(response.status_code, 200)
        self.assert_metric(response.text, "Pendentes", 2)
        self.assert_metric(response.text, "Falhas", 1)
        self.assert_metric(response.text, "SLA em risco", 1)
        self.assert_metric(response.text, "SLA vencido", 1)
        self.assert_metric(response.text, "Sem responsável", 2)

    def test_filters_are_combined(self) -> None:
        response = self.client.get(
            "/admin/operacao",
            params={
                "q": "failure",
                "status": "failed",
                "event": "ticket_update",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("failure@example.com", response.text)
        self.assertNotIn("pending@example.com", response.text)
        self.assertNotIn("sent@example.com", response.text)
        self.assertRegex(response.text, r">\s*1 entrega\s*</p>")
        self.assertRegex(
            response.text,
            r'<option value="failed"\s+selected>',
        )
        self.assertRegex(
            response.text,
            r'<option value="ticket_update"\s+selected>',
        )

    def test_pagination_returns_second_page_and_clamps_large_page(self) -> None:
        db = self.Session()
        try:
            base_time = utcnow() + timedelta(days=1)
            ticket_id = db.query(Ticket.id).order_by(Ticket.id.asc()).first()[0]
            db.add_all(
                [
                    self._notification(
                        ticket_id=ticket_id,
                        event_type="ticket_received",
                        recipient=f"page-{index:02d}@example.com",
                        status="pending",
                        created_at=base_time + timedelta(seconds=index),
                    )
                    for index in range(30)
                ]
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get("/admin/operacao", params={"page": 999})

        self.assertEqual(response.status_code, 200)
        self.assertIn("page-00@example.com", response.text)
        self.assertNotIn("page-29@example.com", response.text)
        self.assertRegex(
            response.text,
            r"Página\s*<strong[^>]*>2</strong>\s*"
            r"de\s*<strong[^>]*>2</strong>",
        )

    def test_retry_resets_failure_and_creates_audit_event(self) -> None:
        response = self.client.post(
            f"/admin/operacao/notificacoes/{self.failed_id}/repetir",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/admin/operacao?status=failed&repetida=1",
        )
        db = self.Session()
        try:
            notification = db.get(NotificationOutbox, self.failed_id)
            self.assertEqual(notification.status, "pending")
            self.assertEqual(notification.attempts, 0)
            self.assertIsNone(notification.last_error)
            self.assertIsNone(notification.locked_at)
            self.assertIsNone(notification.lock_token)
            self.assertIsNone(notification.sent_at)
            self.assertIsNotNone(notification.next_attempt_at)

            events = (
                db.query(AuditEvent)
                .filter(
                    AuditEvent.action == "notification.retry_requested",
                    AuditEvent.resource_type == "notification",
                    AuditEvent.resource_id == str(self.failed_id),
                )
                .all()
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].actor_id, self.admin_id)
            self.assertEqual(events[0].ticket_id, notification.ticket_id)
        finally:
            db.close()

    def test_retry_and_audit_are_rolled_back_together_on_failure(self) -> None:
        with patch(
            "routes.operations.record_event",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                self.client.post(
                    f"/admin/operacao/notificacoes/{self.failed_id}/repetir",
                    follow_redirects=False,
                )

        db = self.Session()
        try:
            notification = db.get(NotificationOutbox, self.failed_id)
            self.assertEqual(notification.status, "failed")
            self.assertEqual(notification.attempts, 5)
            self.assertEqual(
                notification.last_error,
                "Falha temporaria do transporte",
            )
            self.assertEqual(notification.lock_token, "worker-token")
            self.assertEqual(db.query(AuditEvent).count(), 0)
        finally:
            db.close()

    def test_sent_notification_cannot_be_retried(self) -> None:
        response = self.client.post(
            f"/admin/operacao/notificacoes/{self.sent_id}/repetir",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 409)
        db = self.Session()
        try:
            notification = db.get(NotificationOutbox, self.sent_id)
            self.assertEqual(notification.status, "sent")
            self.assertEqual(notification.attempts, 1)
            self.assertEqual(db.query(AuditEvent).count(), 0)
        finally:
            db.close()

    def test_search_payload_is_safe_and_invalid_filters_do_not_explode(self) -> None:
        payload = "%' OR 1=1 -- <script>alert(1)</script>"

        response = self.client.get(
            "/admin/operacao",
            params={
                "q": payload,
                "status": "failed' OR 1=1 --",
                "event": "ticket_update); DROP TABLE audit_events; --",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)
        self.assertRegex(response.text, r">\s*0 entregas\s*</p>")
        db = self.Session()
        try:
            self.assertEqual(
                db.query(NotificationOutbox).count(),
                self.initial_delivery_count,
            )
            self.assertEqual(db.query(AuditEvent).count(), 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
