"""Testes dos indicadores e exportações gerenciais."""
from __future__ import annotations

import unittest
from datetime import datetime
from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, Ticket, TicketPriority, TicketStatus, User
from routes.management import _filtered_tickets, _hours, export_tickets


class ManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.user = User(email="user@example.com", full_name="Pessoa Teste", role="administrador")
        self.db.add(self.user)
        self.db.flush()
        self.db.add(Ticket(subject="Falha", body="Detalhes", requester_id=self.user.id, status=TicketStatus.ABERTO, priority=TicketPriority.ALTA, category="Acesso", hub="BH"))
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_filters_combine_category_and_hub(self) -> None:
        found = _filtered_tickets(self.db, category="Acesso", hub="BH").all()
        missing = _filtered_tickets(self.db, category="Acesso", hub="SP").all()
        self.assertEqual(len(found), 1)
        self.assertEqual(missing, [])

    def test_csv_and_xlsx_export_have_the_same_filtered_ticket(self) -> None:
        csv_response = export_tickets(format="csv", category="Acesso", hub="BH", current_user=self.user, db=self.db)
        xlsx_response = export_tickets(format="xlsx", category="Acesso", hub="BH", current_user=self.user, db=self.db)
        self.assertIn("Falha", csv_response.body.decode("utf-8-sig"))
        workbook = load_workbook(BytesIO(xlsx_response.body), read_only=True)
        self.assertEqual(workbook.active["B2"].value, "Falha")

    def test_elapsed_hours_never_returns_negative_values(self) -> None:
        start = datetime(2026, 1, 1, 10)
        self.assertEqual(_hours(datetime(2026, 1, 1, 12), start), 1.0)
        self.assertEqual(_hours(datetime(2026, 1, 1, 9), start), 0.0)


if __name__ == "__main__":
    unittest.main()
