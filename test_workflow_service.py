"""Testes das regras de transição, reabertura e SLA."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from business_time import add_business_hours, business_hours_between
from database import Ticket, TicketPriority, TicketStatus
from workflow_service import (
    WorkflowError,
    allowed_statuses,
    handle_requester_reply,
    initialize_sla,
    mark_first_response,
    sla_snapshot,
    transition_ticket,
)


def _ticket(
    *,
    status: TicketStatus = TicketStatus.ABERTO,
    priority: TicketPriority = TicketPriority.MEDIA,
    now: datetime | None = None,
) -> Ticket:
    now = now or datetime(2026, 7, 29, 9, 0)
    ticket = Ticket(
        subject="Teste de fluxo",
        body="Descrição",
        requester_id=1,
        status=status,
        priority=priority,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        source_channel="portal",
    )
    initialize_sla(ticket, now=now)
    return ticket


class WorkflowTransitionTests(unittest.TestCase):
    def test_staff_only_receives_valid_next_steps(self) -> None:
        ticket = _ticket(status=TicketStatus.ABERTO)

        self.assertEqual(
            set(allowed_statuses(ticket, requester=False)),
            {
                TicketStatus.EM_TRIAGEM,
                TicketStatus.EM_ANDAMENTO,
                TicketStatus.CANCELADO,
            },
        )

    def test_invalid_transition_does_not_change_ticket(self) -> None:
        ticket = _ticket(status=TicketStatus.ABERTO)

        with self.assertRaisesRegex(WorkflowError, "Não é permitido"):
            transition_ticket(
                ticket,
                TicketStatus.CONCLUIDO,
                requester=False,
            )

        self.assertEqual(ticket.status, TicketStatus.ABERTO)

    def test_block_resolve_cancel_and_reopen_require_a_note(self) -> None:
        ticket = _ticket(status=TicketStatus.EM_ANDAMENTO)

        with self.assertRaisesRegex(WorkflowError, "pelo menos 5"):
            transition_ticket(
                ticket,
                TicketStatus.RESOLVIDO,
                requester=False,
                note="ok",
            )

        self.assertEqual(ticket.status, TicketStatus.EM_ANDAMENTO)

    def test_waiting_pauses_sla_and_requester_reply_resumes_it(self) -> None:
        start = datetime(2026, 7, 29, 9, 0)
        ticket = _ticket(status=TicketStatus.EM_ANDAMENTO, now=start)
        original_first_due = ticket.first_response_due_at
        original_due = ticket.resolution_due_at

        transition_ticket(
            ticket,
            TicketStatus.AGUARDANDO_SOLICITANTE,
            requester=False,
            now=start + timedelta(hours=2),
        )
        self.assertEqual(ticket.sla_paused_at, start + timedelta(hours=2))
        first_remaining = business_hours_between(
            ticket.sla_paused_at, original_first_due
        )
        resolution_remaining = business_hours_between(
            ticket.sla_paused_at, original_due
        )

        event = handle_requester_reply(
            ticket,
            now=start + timedelta(hours=7),
        )

        self.assertEqual(ticket.status, TicketStatus.EM_ANDAMENTO)
        self.assertIsNone(ticket.sla_paused_at)
        self.assertEqual(
            business_hours_between(start + timedelta(hours=7), ticket.resolution_due_at),
            resolution_remaining,
        )
        self.assertEqual(
            business_hours_between(start + timedelta(hours=7), ticket.first_response_due_at),
            first_remaining,
        )
        self.assertIn("Resposta do solicitante", event or "")

    def test_requester_reply_reopens_a_resolved_ticket(self) -> None:
        start = datetime(2026, 7, 29, 12, 0)
        ticket = _ticket(status=TicketStatus.EM_ANDAMENTO, now=start)
        transition_ticket(
            ticket,
            TicketStatus.RESOLVIDO,
            requester=False,
            note="Correção aplicada e validada.",
            now=start + timedelta(hours=3),
        )

        event = handle_requester_reply(
            ticket,
            now=start + timedelta(hours=5),
        )

        self.assertEqual(ticket.status, TicketStatus.REABERTO)
        self.assertIsNone(ticket.resolved_at)
        self.assertIsNone(ticket.closed_at)
        self.assertGreater(ticket.resolution_due_at, start + timedelta(hours=5))
        self.assertIn("Reaberto", event or "")

    def test_requester_can_reopen_a_cancelled_ticket(self) -> None:
        ticket = _ticket(status=TicketStatus.CANCELADO)

        self.assertEqual(
            allowed_statuses(ticket, requester=True),
            [TicketStatus.REABERTO],
        )
        transition_ticket(
            ticket,
            TicketStatus.REABERTO,
            requester=True,
            note="A demanda ainda é necessária.",
        )
        self.assertEqual(ticket.status, TicketStatus.REABERTO)

    def test_first_public_staff_response_is_recorded_once(self) -> None:
        start = datetime(2026, 7, 29, 9, 0)
        ticket = _ticket(now=start)

        self.assertTrue(mark_first_response(ticket, now=start + timedelta(hours=1)))
        self.assertFalse(mark_first_response(ticket, now=start + timedelta(hours=2)))
        self.assertEqual(ticket.first_response_at, start + timedelta(hours=1))
        self.assertEqual(ticket.last_activity_at, start + timedelta(hours=2))


class SlaSnapshotTests(unittest.TestCase):
    def test_snapshot_distinguishes_risk_and_overdue(self) -> None:
        start = datetime(2026, 7, 29, 12, 0)
        ticket = _ticket(
            priority=TicketPriority.URGENTE,
            now=start,
        )

        at_risk = sla_snapshot(ticket, now=start + timedelta(minutes=30))
        overdue = sla_snapshot(ticket, now=start + timedelta(hours=2))

        self.assertEqual(at_risk["state"], "risk")
        self.assertEqual(overdue["state"], "overdue")


class BusinessCalendarTests(unittest.TestCase):
    def test_friday_evening_deadline_moves_to_monday(self) -> None:
        # 21h UTC = 18h em São Paulo.
        friday_evening = datetime(2026, 7, 31, 21, 0)
        due = add_business_hours(friday_evening, 8)
        self.assertEqual(due, datetime(2026, 8, 3, 19, 0))

    def test_weekend_is_not_counted_in_elapsed_indicator(self) -> None:
        friday = datetime(2026, 7, 31, 20, 0)  # 17h local
        monday = datetime(2026, 8, 3, 12, 0)  # 9h local
        self.assertEqual(business_hours_between(friday, monday), 2.0)


if __name__ == "__main__":
    unittest.main()
