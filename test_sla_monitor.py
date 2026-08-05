"""Testes isolados do monitor de SLA e de sua integracao com a outbox."""
from __future__ import annotations

import json
import unittest
from datetime import timedelta

from sqlalchemy import create_engine
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
from sla_monitor import process_sla_alerts


class SlaMonitorTests(unittest.TestCase):
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
        self.now = utcnow().replace(microsecond=0)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _user(
        self,
        email: str,
        *,
        role: str = "solicitante",
        active: bool = True,
    ) -> User:
        db = self.Session()
        try:
            user = User(
                email=email,
                full_name=email.split("@", 1)[0].title(),
                role=role,
                is_active=active,
                is_technician=role in {"tecnico", "administrador"},
            )
            db.add(user)
            db.commit()
            return user
        finally:
            db.close()

    def _ticket(
        self,
        requester: User,
        *,
        assignee: User | None = None,
        status: TicketStatus = TicketStatus.ABERTO,
        first_due_offset: timedelta | None = timedelta(minutes=30),
        resolution_due_offset: timedelta | None = timedelta(days=5),
        first_response_done: bool = False,
        paused: bool = False,
        body: str = "CONTEUDO-SENSIVEL-DA-DEMANDA",
    ) -> Ticket:
        db = self.Session()
        try:
            ticket = Ticket(
                subject="ASSUNTO-SENSIVEL-DA-DEMANDA",
                body=body,
                status=status,
                priority=TicketPriority.MEDIA,
                requester_id=requester.id,
                assignee_id=assignee.id if assignee else None,
                first_response_due_at=(
                    self.now + first_due_offset
                    if first_due_offset is not None
                    else None
                ),
                resolution_due_at=(
                    self.now + resolution_due_offset
                    if resolution_due_offset is not None
                    else None
                ),
                first_response_at=(
                    self.now - timedelta(minutes=5)
                    if first_response_done
                    else None
                ),
                sla_paused_at=(
                    self.now - timedelta(minutes=1) if paused else None
                ),
            )
            db.add(ticket)
            db.commit()
            return ticket
        finally:
            db.close()

    def _outbox(self) -> list[NotificationOutbox]:
        db = self.Session()
        try:
            return (
                db.query(NotificationOutbox)
                .order_by(NotificationOutbox.id.asc())
                .all()
            )
        finally:
            db.close()

    def test_idempotency_and_generic_payload_for_active_assignee(self) -> None:
        requester = self._user("requester@example.com")
        assignee = self._user("technician@example.com", role="tecnico")
        ticket = self._ticket(requester, assignee=assignee)

        first = process_sla_alerts(
            self.Session,
            now=self.now,
            risk_window_minutes=60,
        )
        second = process_sla_alerts(
            self.Session,
            now=self.now,
            risk_window_minutes=60,
        )

        items = self._outbox()
        self.assertEqual(first["queued"], 1)
        self.assertEqual(first["risk"], 1)
        self.assertEqual(second["queued"], 0)
        self.assertEqual(second["deduplicated"], 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].recipient, assignee.email)
        self.assertEqual(items[0].ticket_id, ticket.id)
        self.assertEqual(items[0].event_type, "sla_warning")
        self.assertIn(f"sla:{ticket.id}:first_response:risk:", items[0].dedupe_key)
        self.assertIn(f"recipient-{assignee.id}", items[0].dedupe_key)

        payload_text = items[0].payload_json
        payload = json.loads(payload_text)
        self.assertEqual(payload["subject"], "Alerta operacional de SLA")
        self.assertNotIn(requester.email, payload_text)
        self.assertNotIn(assignee.email, payload_text)
        self.assertNotIn("CONTEUDO-SENSIVEL", payload_text)
        self.assertNotIn("ASSUNTO-SENSIVEL", payload_text)
        db = self.Session()
        try:
            audits = (
                db.query(AuditEvent)
                .filter(AuditEvent.action == "ticket.sla.alert_queued")
                .all()
            )
            self.assertEqual(len(audits), 1)
            self.assertNotIn("CONTEUDO-SENSIVEL", audits[0].changes_json)
            self.assertNotIn("ASSUNTO-SENSIVEL", audits[0].changes_json)
        finally:
            db.close()

    def test_unassigned_ticket_notifies_all_active_admins_only(self) -> None:
        requester = self._user("requester@example.com")
        admin_one = self._user("admin.one@example.com", role="administrador")
        admin_two = self._user("admin.two@example.com", role="administrador")
        self._user(
            "inactive.admin@example.com",
            role="administrador",
            active=False,
        )
        self._user("other.technician@example.com", role="tecnico")
        self._ticket(requester)

        result = process_sla_alerts(
            self.Session,
            now=self.now,
            risk_window_minutes=60,
        )

        recipients = {item.recipient for item in self._outbox()}
        self.assertEqual(result["queued"], 2)
        self.assertEqual(recipients, {admin_one.email, admin_two.email})
        self.assertNotIn(requester.email, recipients)

    def test_requester_is_never_used_even_if_assigned(self) -> None:
        requester_admin = self._user(
            "requester.admin@example.com",
            role="administrador",
        )
        fallback_admin = self._user(
            "fallback.admin@example.com",
            role="administrador",
        )
        self._ticket(requester_admin, assignee=requester_admin)

        process_sla_alerts(
            self.Session,
            now=self.now,
            risk_window_minutes=60,
        )

        recipients = {item.recipient for item in self._outbox()}
        self.assertEqual(recipients, {fallback_admin.email})

    def test_terminal_paused_and_completed_first_response_are_ignored(self) -> None:
        requester = self._user("requester@example.com")
        assignee = self._user("technician@example.com", role="tecnico")
        for status in (
            TicketStatus.RESOLVIDO,
            TicketStatus.CONCLUIDO,
            TicketStatus.CANCELADO,
        ):
            self._ticket(
                requester,
                assignee=assignee,
                status=status,
                first_due_offset=timedelta(minutes=-1),
                resolution_due_offset=timedelta(minutes=-1),
            )
        self._ticket(
            requester,
            assignee=assignee,
            paused=True,
            first_due_offset=timedelta(minutes=-1),
            resolution_due_offset=timedelta(minutes=-1),
        )
        self._ticket(
            requester,
            assignee=assignee,
            first_response_done=True,
            first_due_offset=timedelta(minutes=-1),
            resolution_due_offset=timedelta(days=5),
        )

        result = process_sla_alerts(
            self.Session,
            now=self.now,
            risk_window_minutes=60,
        )

        self.assertEqual(result["queued"], 0)
        self.assertEqual(self._outbox(), [])

    def test_exact_deadline_is_overdue_and_window_limit_is_risk(self) -> None:
        requester = self._user("requester@example.com")
        assignee = self._user("technician@example.com", role="tecnico")
        overdue = self._ticket(
            requester,
            assignee=assignee,
            first_due_offset=timedelta(0),
        )
        risk = self._ticket(
            requester,
            assignee=assignee,
            first_due_offset=timedelta(minutes=60),
        )
        outside = self._ticket(
            requester,
            assignee=assignee,
            first_due_offset=timedelta(minutes=60, seconds=1),
        )

        result = process_sla_alerts(
            self.Session,
            now=self.now,
            risk_window_minutes=60,
        )

        items = self._outbox()
        keys = {item.ticket_id: item.dedupe_key for item in items}
        self.assertEqual(result["queued"], 2)
        self.assertEqual(result["overdue"], 1)
        self.assertEqual(result["risk"], 1)
        event_types = {item.ticket_id: item.event_type for item in items}
        self.assertEqual(event_types[overdue.id], "sla_overdue")
        self.assertEqual(event_types[risk.id], "sla_warning")
        self.assertIn(":first_response:overdue:", keys[overdue.id])
        self.assertIn(":first_response:risk:", keys[risk.id])
        self.assertNotIn(outside.id, keys)

    def test_resolution_milestone_after_first_response(self) -> None:
        requester = self._user("requester@example.com")
        assignee = self._user("technician@example.com", role="tecnico")
        ticket = self._ticket(
            requester,
            assignee=assignee,
            first_response_done=True,
            first_due_offset=timedelta(minutes=-10),
            resolution_due_offset=timedelta(minutes=10),
        )

        result = process_sla_alerts(
            self.Session,
            now=self.now,
            risk_window_minutes=60,
        )

        items = self._outbox()
        self.assertEqual(result["queued"], 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].ticket_id, ticket.id)
        self.assertIn(":resolution:risk:", items[0].dedupe_key)


if __name__ == "__main__":
    unittest.main()
