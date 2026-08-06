from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import automation_worker
from database import Base, ReportSchedule, SlaPolicy, Ticket, TicketPriority, User
from routes.features import _slug
from workflow_service import initialize_sla


class OperationalExtensionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.user = User(email="admin@example.com", full_name="Admin", role="administrador")
        self.db.add(self.user)
        self.db.flush()

    def tearDown(self):
        self.db.close()

    def test_slug_is_safe_and_readable(self):
        self.assertEqual(_slug("Acesso à VPN / Escritório"), "acesso-a-vpn-escritorio")

    def test_specific_sla_policy_overrides_default(self):
        self.db.add(SlaPolicy(name="VPN urgente", priority="Urgente", category="Acesso", first_response_hours=2, resolution_hours=6))
        self.db.commit()
        created = datetime(2026, 8, 10, 12, 0)
        ticket = Ticket(subject="VPN", body="Sem acesso", requester_id=self.user.id, priority=TicketPriority.URGENTE, category="Acesso", created_at=created)
        initialize_sla(ticket, now=created, db=self.db)
        self.assertGreater(ticket.resolution_due_at, ticket.first_response_due_at)

    def test_due_report_advances_only_after_attempt(self):
        schedule = ReportSchedule(name="Resumo", recipient="admin@example.com", frequency="daily", report_format="csv", filters_json="{}", next_run_at=datetime(2020, 1, 1), created_by_id=self.user.id)
        self.db.add(schedule)
        self.db.commit()
        original = automation_worker.SessionLocal
        automation_worker.SessionLocal = self.Session
        try:
            with patch.object(automation_worker, "send_custom_email", return_value=True):
                automation_worker.scheduled_reports_cycle()
            self.db.expire_all()
            self.assertEqual(self.db.get(ReportSchedule, schedule.id).last_status, "sent")
        finally:
            automation_worker.SessionLocal = original


if __name__ == "__main__":
    unittest.main()
