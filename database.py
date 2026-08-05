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
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    inspect,
    or_,
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


def utcnow() -> datetime:
    """UTC sem timezone para manter compatibilidade com colunas legadas."""
    return datetime.now(UTC).replace(tzinfo=None)

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


if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Enums de domínio
# --------------------------------------------------------------------------
class TicketStatus(str, enum.Enum):
    ABERTO = "Aberto"
    EM_TRIAGEM = "Em Triagem"
    EM_ANDAMENTO = "Em Andamento"
    AGUARDANDO_SOLICITANTE = "Aguardando Solicitante"
    BLOQUEADO = "Bloqueado"
    RESOLVIDO = "Resolvido"
    CONCLUIDO = "Concluído"
    CANCELADO = "Cancelado"
    REABERTO = "Reaberto"


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

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

    # Projeto vinculado (vem da API corporativa de projetos). Fica nulo em
    # tickets abertos por e-mail, onde não há como o solicitante escolher.
    project_code: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    hub: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_channel: Mapped[str] = mapped_column(
        String(20), default="portal", nullable=False, index=True
    )

    # E-mail original (para rastreabilidade / evitar duplicidade)
    source_message_id: Mapped[str | None] = mapped_column(String(500), unique=True, nullable=True)

    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, index=True
    )
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_response_due_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    resolution_due_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    sla_paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    # Notas internas ficam visíveis apenas para técnico/administrador.
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    source_message_id: Mapped[str | None] = mapped_column(
        String(500), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    ticket: Mapped["Ticket"] = relationship(back_populates="attachments")
    comment: Mapped["Comment | None"] = relationship(back_populates="attachments")
    uploader: Mapped["User"] = relationship(back_populates="attachments")


class Category(Base):
    """Categoria administrável usada na triagem e no portal."""

    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class CannedResponse(Base):
    """Resposta pronta compartilhada pela equipe."""

    __tablename__ = "canned_responses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class InAppNotification(Base):
    """Notificação interna persistente e individual."""

    __tablename__ = "in_app_notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)


class SatisfactionRating(Base):
    """Avaliação única do solicitante após a resolução."""

    __tablename__ = "satisfaction_ratings"
    __table_args__ = (Index("ux_satisfaction_ticket", "ticket_id", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class TypingPresence(Base):
    """Presença efêmera compartilhada entre processos web."""

    __tablename__ = "typing_presence"
    __table_args__ = (Index("ux_typing_ticket_user", "ticket_id", "user_id", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    user_name: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)


class NotificationOutbox(Base):
    """Notificacao duravel, enviada de forma assincrona apos o commit."""

    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index(
            "ix_notification_outbox_status_next_attempt_at",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ux_notification_outbox_dedupe_key",
            "dedupe_key",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Chaves opcionais tornam alertas recorrentes idempotentes sem impedir
    # notificacoes normais (SQLite e PostgreSQL aceitam varios NULLs).
    dedupe_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        default=lambda: settings.NOTIFICATION_OUTBOX_MAX_ATTEMPTS,
        nullable=False,
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class AuditEvent(Base):
    """Evento estruturado e imutavel para rastreabilidade administrativa."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index(
            "ix_audit_events_ticket_created_id",
            "ticket_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_audit_events_action_created",
            "action",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_uuid: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        nullable=False,
        index=True,
    )
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    changes_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        nullable=False,
        index=True,
    )


@event.listens_for(AuditEvent, "before_update")
def _prevent_audit_event_update(_mapper, _connection, _target) -> None:
    raise ValueError("Eventos de auditoria nao podem ser alterados.")


@event.listens_for(AuditEvent, "before_delete")
def _prevent_audit_event_delete(_mapper, _connection, _target) -> None:
    raise ValueError("Eventos de auditoria nao podem ser excluidos.")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def init_db() -> None:
    """Cria as tabelas e semeia usuários de exemplo em DEV_MODE."""
    Base.metadata.create_all(bind=engine)
    _ensure_postgres_ticket_status_values()
    _ensure_user_access_columns()
    _ensure_ticket_project_columns()
    _ensure_ticket_workflow_columns()
    _ensure_comment_visibility_column()
    _ensure_comment_email_thread_column()
    _ensure_notification_outbox_schema()
    _ensure_ticket_workflow_indexes()
    _backfill_ticket_workflow_data()
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
            technician_filter = (
                "is_technician IS TRUE"
                if engine.dialect.name == "postgresql"
                else "is_technician = 1"
            )
            conn.execute(
                text(
                    "UPDATE users SET role = 'tecnico' "
                    f"WHERE {technician_filter}"
                )
            )


def _ensure_ticket_project_columns() -> None:
    """Migração leve para bancos criados antes do vínculo com projetos."""
    columns = {column["name"] for column in inspect(engine).get_columns("tickets")}
    if "project_code" in columns and "project_name" in columns:
        return
    with engine.begin() as conn:
        if "project_code" not in columns:
            conn.execute(text("ALTER TABLE tickets ADD COLUMN project_code INTEGER"))
        if "project_name" not in columns:
            conn.execute(
                text("ALTER TABLE tickets ADD COLUMN project_name VARCHAR(255)")
            )


def _ensure_postgres_ticket_status_values() -> None:
    """Atualiza o enum nativo quando o banco existente é PostgreSQL."""
    if engine.dialect.name != "postgresql":
        return
    enum_name = Ticket.__table__.c.status.type.name
    if not enum_name:
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for status in TicketStatus:
            conn.execute(
                text(
                    f'ALTER TYPE "{enum_name}" '
                    f"ADD VALUE IF NOT EXISTS '{status.name}'"
                )
            )


def _ensure_ticket_workflow_columns() -> None:
    """Migração leve para os campos de triagem, SLA e última atividade."""
    columns = {column["name"] for column in inspect(engine).get_columns("tickets")}
    datetime_type = "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"
    definitions = {
        "category": "VARCHAR(120)",
        "hub": "VARCHAR(255)",
        "source_channel": "VARCHAR(20) NOT NULL DEFAULT 'portal'",
        "last_activity_at": datetime_type,
        "first_response_at": datetime_type,
        "first_response_due_at": datetime_type,
        "resolution_due_at": datetime_type,
        "sla_paused_at": datetime_type,
        "resolved_at": datetime_type,
        "closed_at": datetime_type,
        "resolution_summary": "TEXT",
    }
    missing = {
        name: definition
        for name, definition in definitions.items()
        if name not in columns
    }
    if not missing:
        return
    with engine.begin() as conn:
        for name, definition in missing.items():
            conn.execute(
                text(f'ALTER TABLE tickets ADD COLUMN "{name}" {definition}')
            )


def _ensure_comment_visibility_column() -> None:
    """Adiciona o marcador de nota interna em bancos existentes."""
    columns = {column["name"] for column in inspect(engine).get_columns("comments")}
    if "is_internal" in columns:
        return
    boolean_type = "BOOLEAN" if engine.dialect.name == "postgresql" else "INTEGER"
    boolean_default = "FALSE" if engine.dialect.name == "postgresql" else "0"
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE comments ADD COLUMN is_internal "
                f"{boolean_type} NOT NULL DEFAULT {boolean_default}"
            )
        )


def _ensure_comment_email_thread_column() -> None:
    """Adiciona idempotência para respostas recebidas por e-mail."""
    columns = {column["name"] for column in inspect(engine).get_columns("comments")}
    with engine.begin() as conn:
        if "source_message_id" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE comments ADD COLUMN source_message_id VARCHAR(500)"
                )
            )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ux_comments_source_message_id ON comments (source_message_id)"
            )
        )


def _ensure_notification_outbox_schema() -> None:
    """Completa a tabela da outbox em instalacoes atualizadas sem Alembic."""
    inspector = inspect(engine)
    if not inspector.has_table("notification_outbox"):
        # Em uma instalacao nova, create_all acima ja cria a tabela completa.
        NotificationOutbox.__table__.create(bind=engine, checkfirst=True)
        return

    columns = {
        column["name"]
        for column in inspector.get_columns("notification_outbox")
    }
    datetime_type = "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"
    definitions = {
        "ticket_id": (
            "INTEGER REFERENCES tickets(id) ON DELETE SET NULL"
        ),
        "event_type": "VARCHAR(50)",
        "recipient": "VARCHAR(255)",
        "payload_json": "TEXT",
        "dedupe_key": "VARCHAR(500)",
        "status": "VARCHAR(20) NOT NULL DEFAULT 'pending'",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "max_attempts": (
            "INTEGER NOT NULL DEFAULT "
            f"{max(1, settings.NOTIFICATION_OUTBOX_MAX_ATTEMPTS)}"
        ),
        "next_attempt_at": datetime_type,
        "locked_at": datetime_type,
        "lock_token": "VARCHAR(64)",
        "sent_at": datetime_type,
        "last_error": "VARCHAR(500)",
        "created_at": datetime_type,
        "updated_at": datetime_type,
    }
    with engine.begin() as conn:
        for name, definition in definitions.items():
            if name not in columns:
                conn.execute(
                    text(
                        'ALTER TABLE notification_outbox '
                        f'ADD COLUMN "{name}" {definition}'
                    )
                )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_notification_outbox_ticket_id "
                "ON notification_outbox (ticket_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_notification_outbox_status_next_attempt_at "
                "ON notification_outbox (status, next_attempt_at)"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ux_notification_outbox_dedupe_key "
                "ON notification_outbox (dedupe_key)"
            )
        )


def _ensure_ticket_workflow_indexes() -> None:
    """Cria os índices usados pelas filas e pelos alertas de SLA."""
    indexes = {
        "ix_tickets_last_activity_at": "last_activity_at",
        "ix_tickets_first_response_due_at": "first_response_due_at",
        "ix_tickets_resolution_due_at": "resolution_due_at",
        "ix_tickets_category": "category",
        "ix_tickets_hub": "hub",
        "ix_tickets_source_channel": "source_channel",
        "ix_tickets_requester_id": "requester_id",
        "ix_tickets_assignee_id": "assignee_id",
    }
    with engine.begin() as conn:
        for index_name, column_name in indexes.items():
            conn.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                    f'ON tickets ("{column_name}")'
                )
            )


def _backfill_ticket_workflow_data() -> None:
    """Preenche campos derivados sem sobrescrever dados já migrados."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE tickets "
                "SET last_activity_at = COALESCE(updated_at, created_at) "
                "WHERE last_activity_at IS NULL"
            )
        )
        conn.execute(
            text(
                "UPDATE tickets "
                "SET source_channel = CASE "
                "WHEN source_message_id IS NOT NULL THEN 'email' "
                "ELSE COALESCE(NULLIF(source_channel, ''), 'portal') END "
                "WHERE source_message_id IS NOT NULL "
                "OR source_channel IS NULL OR source_channel = ''"
            )
        )
    from workflow_service import initialize_sla

    db = SessionLocal()
    try:
        tickets = (
            db.query(Ticket)
            .filter(
                or_(
                    Ticket.first_response_due_at.is_(None),
                    Ticket.resolution_due_at.is_(None),
                )
            )
            .all()
        )
        for ticket in tickets:
            initialize_sla(ticket)
        if tickets:
            db.commit()
    finally:
        db.close()


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
                role="tecnico",
                is_technician=True,
            ),
        ]
        db.add_all(seed_users)
        db.commit()
    finally:
        db.close()
