"""Testes das funcionalidades de maturidade e produto."""
from __future__ import annotations

import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import email_worker
from database import Base, SatisfactionRating, Ticket, TicketStatus, User
from routes.api import SatisfactionIn, rate_ticket


class ProductFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.user = User(email="requester@example.com", full_name="Solicitante", role="solicitante", is_active=True)
        self.db.add(self.user)
        self.db.flush()
        self.ticket = Ticket(subject="Atendimento", body="Descrição suficiente", requester_id=self.user.id, status=TicketStatus.CONCLUIDO)
        self.db.add(self.ticket)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_requester_can_rate_completed_ticket_only_once(self) -> None:
        result = rate_ticket(self.ticket.id, SatisfactionIn(score=5), self.user, self.db)
        self.assertEqual(result["score"], 5)
        self.assertEqual(self.db.query(SatisfactionRating).count(), 1)
        with self.assertRaises(Exception):
            rate_ticket(self.ticket.id, SatisfactionIn(score=4), self.user, self.db)

    def test_email_reply_removes_quoted_history_and_signature(self) -> None:
        body = "Resposta útil.\n\n--\nAssinatura\n> histórico"
        self.assertEqual(email_worker._clean_reply_body(body), "Resposta útil.")


if __name__ == "__main__":
    unittest.main()
