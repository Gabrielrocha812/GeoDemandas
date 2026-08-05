"""Criação simples de notificações internas na transação do chamador."""
from sqlalchemy.orm import Session

from database import InAppNotification, Ticket, User


def notify(db: Session, user: User | None, ticket: Ticket, title: str, message: str) -> None:
    if user is None or not user.is_active:
        return
    db.add(InAppNotification(user_id=user.id, ticket_id=ticket.id, title=title[:160], message=message[:500]))
