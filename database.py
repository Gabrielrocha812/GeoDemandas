"""
database.py
-----------
Configuração do SQLAlchemy: engine, sessão e modelos (Users, Tickets,
Comments). Chame `init_db()` na inicialização para criar as tabelas.

Modelo de dados:
    User    1 --- N  Ticket   (requester_id / assignee_id)
    Ticket  1 --- N  Comment
    User    1 --- N  Comment
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from config import settings

# --------------------------------------------------------------------------
# Engine / Session
# --------------------------------------------------------------------------
# `check_same_thread` só é necessário para SQLite (permite uso pelo worker).
connect_args = (
    {"check_same_thread": False}
    if settings.DATABASE_URL.startswith("sqlite")
    else {}
)

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Enums de domínio
# --------------------------------------------------------------------------
class TicketStatus(str, enum.Enum):
    ABERTO = "Aberto"
    EM_ANDAMENTO = "Em Andamento"
    CONCLUIDO = "Concluído"


class TicketPriority(str, enum.Enum):
    BAIXA = "Baixa"
    MEDIA = "Média"
    ALTA = "Alta"
    URGENTE = "Urgente"


# --------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------
class User(Base):
    """Usuário sincronizado do Active Directory (AD/LDAP)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hubs: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="solicitante", nullable=False)
    is_technician: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tickets_created: Mapped[list["Ticket"]] = relationship(
        back_populates="requester", foreign_keys="Ticket.requester_id"
    )
    comments: Mapped[list["Comment"]] = relationship(back_populates="author")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="uploader")


class Ticket(Base):
    """Demanda/chamado criado a partir de um e-mail validado."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus), default=TicketStatus.ABERTO, index=True
    )
    priority: Mapped[TicketPriority] = mapped_column(
        SAEnum(TicketPriority), default=TicketPriority.MEDIA, index=True
    )

    # E-mail original (para rastreabilidade / evitar duplicidade)
    source_message_id: Mapped[str | None] = mapped_column(String(500), unique=True, nullable=True)

    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    requester: Mapped["User"] = relationship(
        back_populates="tickets_created", foreign_keys=[requester_id]
    )
    assignee: Mapped["User | None"] = relationship(foreign_keys=[assignee_id])
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="Comment.created_at"
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="Attachment.created_at"
    )


class Comment(Base):
    """Comentário/interação ou registro de mudança de status no ticket."""

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # is_system=True para eventos automáticos (ex.: "Status alterado para ...")
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ticket: Mapped["Ticket"] = relationship(back_populates="comments")
    author: Mapped["User"] = relationship(back_populates="comments")
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="comment", cascade="all, delete-orphan"
    )


class Attachment(Base):
    """Arquivo protegido associado à demanda ou a uma mensagem."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False, index=True)
    comment_id: Mapped[int | None] = mapped_column(ForeignKey("comments.id"), nullable=True, index=True)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    original_name: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ticket: Mapped["Ticket"] = relationship(back_populates="attachments")
    comment: Mapped["Comment | None"] = relationship(back_populates="attachments")
    uploader: Mapped["User"] = relationship(back_populates="attachments")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def init_db() -> None:
    """Cria as tabelas e semeia usuários de exemplo em DEV_MODE."""
    Base.metadata.create_all(bind=engine)
    _ensure_user_access_columns()
    if settings.DEV_MODE:
        _seed_dev_data()
    _bootstrap_initial_admin()


def _ensure_user_access_columns() -> None:
    """Migração leve para bancos existentes anteriores ao controle de acesso."""
    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    with engine.begin() as conn:
        if "hubs" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN hubs TEXT"))
        if "role" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN role VARCHAR(20) "
                    "NOT NULL DEFAULT 'solicitante'"
                )
            )
            conn.execute(
                text(
                    "UPDATE users SET role = 'tecnico' "
                    "WHERE is_technician = 1"
                )
            )


def _bootstrap_initial_admin() -> None:
    """Define o primeiro administrador sem sobrescrever escolhas posteriores."""
    emails = {
        email.strip().lower()
        for email in settings.INITIAL_ADMIN_EMAILS.split(",")
        if email.strip()
    }
    if not emails:
        return
    db = SessionLocal()
    try:
        if db.query(User).filter(User.role == "administrador").first():
            return
        user = db.query(User).filter(User.email.in_(emails)).first()
        if user:
            user.role = "administrador"
            user.is_technician = True
            db.commit()
    finally:
        db.close()


def get_db():
    """Dependency do FastAPI: fornece uma sessão e garante o fechamento."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _seed_dev_data() -> None:
    """
    Insere alguns usuários fictícios para testar sem AD real.
    Esses e-mails casam com os mocks de LDAP em ldap_auth.py.
    """
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return
        seed_users = [
            User(
                email="joao.silva@brandt.com.br",
                full_name="João Silva",
                department="Geotecnia",
                is_technician=False,
            ),
            User(
                email="maria.souza@brandt.com.br",
                full_name="Maria Souza",
                department="Meio Ambiente",
                is_technician=False,
            ),
            User(
                email="tecnico.ti@brandt.com.br",
                full_name="Carlos Técnico",
                department="TI",
                is_technician=True,
            ),
        ]
        db.add_all(seed_users)
        db.commit()
    finally:
        db.close()
