"""Regras centrais do ciclo de atendimento e dos indicadores de SLA."""
from __future__ import annotations

from datetime import datetime, timedelta

from config import settings
from business_time import add_business_hours, business_hours_between
from database import Ticket, TicketPriority, TicketStatus, utcnow


class WorkflowError(ValueError):
    """Erro de regra de negócio seguro para exibição ao usuário."""


STAFF_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.ABERTO: {
        TicketStatus.EM_TRIAGEM,
        TicketStatus.EM_ANDAMENTO,
        TicketStatus.CANCELADO,
    },
    TicketStatus.EM_TRIAGEM: {
        TicketStatus.EM_ANDAMENTO,
        TicketStatus.AGUARDANDO_SOLICITANTE,
        TicketStatus.BLOQUEADO,
        TicketStatus.CANCELADO,
    },
    TicketStatus.EM_ANDAMENTO: {
        TicketStatus.AGUARDANDO_SOLICITANTE,
        TicketStatus.BLOQUEADO,
        TicketStatus.RESOLVIDO,
        TicketStatus.CANCELADO,
    },
    TicketStatus.AGUARDANDO_SOLICITANTE: {
        TicketStatus.EM_ANDAMENTO,
        TicketStatus.RESOLVIDO,
        TicketStatus.CANCELADO,
    },
    TicketStatus.BLOQUEADO: {
        TicketStatus.EM_ANDAMENTO,
        TicketStatus.CANCELADO,
    },
    TicketStatus.RESOLVIDO: {
        TicketStatus.CONCLUIDO,
        TicketStatus.REABERTO,
    },
    TicketStatus.CONCLUIDO: {TicketStatus.REABERTO},
    TicketStatus.CANCELADO: {TicketStatus.REABERTO},
    TicketStatus.REABERTO: {
        TicketStatus.EM_TRIAGEM,
        TicketStatus.EM_ANDAMENTO,
        TicketStatus.CANCELADO,
    },
}

REQUESTER_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.ABERTO: {TicketStatus.CANCELADO},
    TicketStatus.EM_TRIAGEM: {TicketStatus.CANCELADO},
    TicketStatus.RESOLVIDO: {
        TicketStatus.CONCLUIDO,
        TicketStatus.REABERTO,
    },
    TicketStatus.CONCLUIDO: {TicketStatus.REABERTO},
    TicketStatus.CANCELADO: {TicketStatus.REABERTO},
}

TERMINAL_STATUSES = {TicketStatus.CONCLUIDO, TicketStatus.CANCELADO}
RESOLUTION_STATUSES = {
    TicketStatus.RESOLVIDO,
    TicketStatus.CONCLUIDO,
    TicketStatus.CANCELADO,
}


def _first_response_hours(priority: TicketPriority) -> int:
    return {
        TicketPriority.BAIXA: settings.SLA_FIRST_RESPONSE_HOURS_BAIXA,
        TicketPriority.MEDIA: settings.SLA_FIRST_RESPONSE_HOURS_MEDIA,
        TicketPriority.ALTA: settings.SLA_FIRST_RESPONSE_HOURS_ALTA,
        TicketPriority.URGENTE: settings.SLA_FIRST_RESPONSE_HOURS_URGENTE,
    }[priority]


def _resolution_hours(priority: TicketPriority) -> int:
    return {
        TicketPriority.BAIXA: settings.SLA_RESOLUTION_HOURS_BAIXA,
        TicketPriority.MEDIA: settings.SLA_RESOLUTION_HOURS_MEDIA,
        TicketPriority.ALTA: settings.SLA_RESOLUTION_HOURS_ALTA,
        TicketPriority.URGENTE: settings.SLA_RESOLUTION_HOURS_URGENTE,
    }[priority]


def initialize_sla(
    ticket: Ticket,
    *,
    now: datetime | None = None,
    reset: bool = False,
) -> None:
    """Inicializa ou recalcula os marcos básicos de SLA da demanda."""
    now = now or utcnow()
    base = ticket.created_at or now
    ticket.last_activity_at = ticket.last_activity_at or base
    if reset or ticket.first_response_due_at is None:
        ticket.first_response_due_at = add_business_hours(
            base, _first_response_hours(ticket.priority)
        )
    if reset or ticket.resolution_due_at is None:
        ticket.resolution_due_at = add_business_hours(
            base, _resolution_hours(ticket.priority)
        )


def touch_ticket(ticket: Ticket, *, now: datetime | None = None) -> datetime:
    """Atualiza a ordenação por atividade mesmo quando só um comentário muda."""
    now = now or utcnow()
    ticket.last_activity_at = now
    ticket.updated_at = now
    return now


def mark_first_response(ticket: Ticket, *, now: datetime | None = None) -> bool:
    """Registra uma única vez a primeira resposta pública da equipe."""
    now = touch_ticket(ticket, now=now)
    if ticket.first_response_at is not None:
        return False
    ticket.first_response_at = now
    return True


def allowed_statuses(ticket: Ticket, *, requester: bool) -> list[TicketStatus]:
    transitions = REQUESTER_TRANSITIONS if requester else STAFF_TRANSITIONS
    return sorted(
        transitions.get(ticket.status, set()),
        key=lambda status: list(TicketStatus).index(status),
    )


def _requires_note(new_status: TicketStatus) -> bool:
    return new_status in {
        TicketStatus.BLOQUEADO,
        TicketStatus.RESOLVIDO,
        TicketStatus.CANCELADO,
        TicketStatus.REABERTO,
    }


def _resume_sla(ticket: Ticket, now: datetime) -> None:
    if ticket.sla_paused_at is None:
        return
    if (
        ticket.first_response_at is None
        and ticket.first_response_due_at is not None
    ):
        remaining = business_hours_between(
            ticket.sla_paused_at, ticket.first_response_due_at
        )
        ticket.first_response_due_at = add_business_hours(now, remaining)
    if ticket.resolution_due_at is not None:
        remaining = business_hours_between(
            ticket.sla_paused_at, ticket.resolution_due_at
        )
        ticket.resolution_due_at = add_business_hours(now, remaining)
    ticket.sla_paused_at = None


def transition_ticket(
    ticket: Ticket,
    new_status: TicketStatus,
    *,
    requester: bool,
    note: str | None = None,
    now: datetime | None = None,
) -> str:
    """Valida e aplica uma transição, devolvendo o texto para a timeline."""
    old_status = ticket.status
    if new_status == old_status:
        raise WorkflowError("A demanda já está nesse status.")
    transitions = REQUESTER_TRANSITIONS if requester else STAFF_TRANSITIONS
    if new_status not in transitions.get(old_status, set()):
        raise WorkflowError(
            f'Não é permitido alterar de "{old_status.value}" '
            f'para "{new_status.value}".'
        )

    clean_note = (note or "").strip()
    if _requires_note(new_status) and len(clean_note) < 5:
        raise WorkflowError(
            "Informe um motivo ou resumo com pelo menos 5 caracteres."
        )

    now = now or utcnow()
    if old_status == TicketStatus.AGUARDANDO_SOLICITANTE:
        _resume_sla(ticket, now)
    if (
        new_status == TicketStatus.AGUARDANDO_SOLICITANTE
        and ticket.sla_paused_at is None
    ):
        ticket.sla_paused_at = now

    ticket.status = new_status
    if new_status == TicketStatus.RESOLVIDO:
        ticket.resolved_at = now
        ticket.resolution_summary = clean_note
        _resume_sla(ticket, now)
    elif new_status == TicketStatus.CONCLUIDO:
        ticket.closed_at = now
        _resume_sla(ticket, now)
    elif new_status == TicketStatus.REABERTO:
        ticket.resolved_at = None
        ticket.closed_at = None
        ticket.sla_paused_at = None
        ticket.resolution_due_at = add_business_hours(
            now, _resolution_hours(ticket.priority)
        )
    elif new_status == TicketStatus.CANCELADO:
        ticket.closed_at = now
        _resume_sla(ticket, now)

    touch_ticket(ticket, now=now)
    content = (
        f'Status alterado de "{old_status.value}" para "{new_status.value}".'
    )
    if clean_note:
        content += f" Motivo/resumo: {clean_note}"
    return content


def handle_requester_reply(
    ticket: Ticket,
    *,
    now: datetime | None = None,
) -> str | None:
    """Move uma demanda de volta à fila ativa quando o solicitante responde."""
    now = now or utcnow()
    old_status = ticket.status
    if old_status == TicketStatus.AGUARDANDO_SOLICITANTE:
        _resume_sla(ticket, now)
        ticket.status = TicketStatus.EM_ANDAMENTO
    elif old_status in {
        TicketStatus.RESOLVIDO,
        TicketStatus.CONCLUIDO,
        TicketStatus.CANCELADO,
    }:
        ticket.status = TicketStatus.REABERTO
        ticket.resolved_at = None
        ticket.closed_at = None
        ticket.sla_paused_at = None
        ticket.resolution_due_at = add_business_hours(
            now, _resolution_hours(ticket.priority)
        )
    else:
        touch_ticket(ticket, now=now)
        return None

    touch_ticket(ticket, now=now)
    return (
        f'Resposta do solicitante alterou automaticamente o status de '
        f'"{old_status.value}" para "{ticket.status.value}".'
    )


def sla_snapshot(ticket: Ticket, *, now: datetime | None = None) -> dict:
    """Retorna um estado simples e serializável para cards e filtros."""
    now = now or utcnow()
    if ticket.status in RESOLUTION_STATUSES:
        return {"state": "done", "label": "SLA finalizado", "due_at": None}
    if ticket.sla_paused_at is not None:
        return {
            "state": "paused",
            "label": "SLA pausado",
            "due_at": ticket.resolution_due_at,
        }

    due_at = (
        ticket.first_response_due_at
        if ticket.first_response_at is None and ticket.first_response_due_at
        else ticket.resolution_due_at
    )
    if due_at is None:
        return {"state": "unknown", "label": "SLA não calculado", "due_at": None}
    if due_at <= now:
        return {"state": "overdue", "label": "SLA vencido", "due_at": due_at}
    if due_at <= add_business_hours(now, settings.SLA_RISK_WINDOW_HOURS):
        return {"state": "risk", "label": "SLA em risco", "due_at": due_at}
    return {"state": "ok", "label": "Dentro do SLA", "due_at": due_at}
