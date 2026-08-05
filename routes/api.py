"""
routes/api.py
-------------
Endpoints JSON usados pelo frontend (Alpine.js) para:
  - adicionar comentários a um ticket;
  - alterar status;
  - atribuir um técnico responsável.

Toda ação exige `get_current_user` -> só usuários presentes e ativos na base
local (sincronizados do AD) conseguem interagir com os chamados.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from audit_service import record_event
from auth import (
    ROLE_ADMIN,
    ROLE_REQUESTER,
    ROLE_TECHNICIAN,
    get_current_user,
    require_roles,
)
from attachment_service import (
    attachment_path,
    delete_saved_uploads,
    is_preview_image,
    is_previewable,
    save_uploads,
)
from database import (
    Attachment,
    CannedResponse,
    Category,
    Comment,
    InAppNotification,
    SatisfactionRating,
    Ticket,
    TicketPriority,
    TicketStatus,
    TypingPresence,
    User,
    get_db,
    utcnow,
)
from in_app_service import notify
from outbox_service import enqueue_ticket_completed, enqueue_ticket_update
from projeto_service import ProjetosUnavailableError, find_projeto
from time_utils import format_local_datetime, iso_utc
from workflow_service import (
    RESOLUTION_STATUSES,
    WorkflowError,
    allowed_statuses,
    handle_requester_reply,
    initialize_sla,
    mark_first_response,
    sla_snapshot,
    touch_ticket,
    transition_ticket,
)

router = APIRouter(prefix="/api", tags=["api"])


# --------------------------------------------------------------------------
# Schemas de entrada/saída
# --------------------------------------------------------------------------
class CommentIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)
    is_internal: bool = False


class StatusIn(BaseModel):
    status: TicketStatus
    note: str | None = Field(default=None, max_length=2000)


class AssigneeIn(BaseModel):
    assignee_id: int | None = None


class TicketMetadataIn(BaseModel):
    priority: TicketPriority | None = None
    project_code: int | None = None
    category: str | None = Field(default=None, max_length=120)
    hub: str | None = Field(default=None, max_length=255)


class TypingIn(BaseModel):
    typing: bool = True


class SatisfactionIn(BaseModel):
    score: int = Field(..., ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


class BulkStatusIn(BaseModel):
    ticket_ids: list[int] = Field(..., min_length=1, max_length=100)
    status: TicketStatus
    note: str | None = Field(default=None, max_length=2000)


class CatalogItemIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)


class CannedResponseIn(BaseModel):
    title: str = Field(..., min_length=2, max_length=120)
    content: str = Field(..., min_length=2, max_length=5000)


class AttachmentOut(BaseModel):
    id: int
    name: str
    content_type: str
    size_bytes: int
    preview_type: str | None
    url: str


class CommentOut(BaseModel):
    id: int
    content: str
    author_name: str
    is_system: bool
    is_internal: bool
    created_at: str
    attachments: list[AttachmentOut] = Field(default_factory=list)


class TicketStateOut(BaseModel):
    status: str
    allowed_statuses: list[str]
    assignee_id: int | None
    assignee_name: str | None
    updated_at: str
    last_activity_at: str
    sla_state: str
    sla_label: str
    sla_due_at: str | None
    comments: list[CommentOut]
    typing_users: list[str]


def _serialize_comment(c: Comment) -> CommentOut:
    return CommentOut(
        id=c.id,
        content=c.content,
        author_name=c.author.full_name,
        is_system=c.is_system,
        is_internal=c.is_internal,
        created_at=format_local_datetime(c.created_at),
        attachments=[_serialize_attachment(item) for item in c.attachments],
    )


def _serialize_attachment(item: Attachment) -> AttachmentOut:
    preview_type = None
    if is_preview_image(item.content_type):
        preview_type = "image"
    elif item.content_type.lower() == "application/pdf":
        preview_type = "pdf"
    return AttachmentOut(
        id=item.id,
        name=item.original_name,
        content_type=item.content_type,
        size_bytes=item.size_bytes,
        preview_type=preview_type,
        url=f"/api/attachments/{item.id}",
    )


def _get_ticket_or_404(db: Session, ticket_id: int) -> Ticket:
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket não encontrado")
    return ticket


def _ensure_ticket_access(ticket: Ticket, current_user: User) -> None:
    if _is_staff(current_user):
        return
    if (
        current_user.role == ROLE_REQUESTER
        and ticket.requester_id == current_user.id
    ):
        return
    raise HTTPException(status_code=404, detail="Ticket não encontrado")


def _is_staff(user: User) -> bool:
    return user.role in {ROLE_ADMIN, ROLE_TECHNICIAN}


def _notification_target(ticket: Ticket, current_user: User) -> User | None:
    """Retorna a outra ponta da conversa, quando houver destinatário."""
    if current_user.id == ticket.requester_id:
        return ticket.assignee
    return ticket.requester


def _append_automatic_reply_event(
    db: Session,
    ticket: Ticket,
    current_user: User,
) -> None:
    content = handle_requester_reply(ticket)
    if content:
        db.add(
            Comment(
                ticket_id=ticket.id,
                author_id=current_user.id,
                content=content,
                is_system=True,
                is_internal=False,
            )
        )


def _apply_message_activity(
    db: Session,
    ticket: Ticket,
    current_user: User,
    *,
    is_internal: bool,
) -> None:
    if is_internal:
        touch_ticket(ticket)
    elif _is_staff(current_user):
        mark_first_response(ticket)
    else:
        _append_automatic_reply_event(db, ticket, current_user)


def _active_typing_users(db: Session, ticket_id: int, current_user_id: int) -> list[str]:
    now = utcnow()
    db.query(TypingPresence).filter(TypingPresence.expires_at <= now).delete(synchronize_session=False)
    return [row[0] for row in db.query(TypingPresence.user_name).filter(TypingPresence.ticket_id == ticket_id, TypingPresence.user_id != current_user_id, TypingPresence.expires_at > now).all()]


@router.get("/tickets/{ticket_id}/state", response_model=TicketStateOut)
def ticket_state(
    ticket_id: int,
    after_comment_id: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_ticket_access(ticket, current_user)
    sla = sla_snapshot(ticket)
    comments_query = db.query(Comment).filter(
        Comment.ticket_id == ticket.id,
        Comment.id > max(0, after_comment_id),
    )
    if not _is_staff(current_user):
        comments_query = comments_query.filter(Comment.is_internal.is_(False))
    comments = comments_query.order_by(Comment.id.asc()).all()
    return TicketStateOut(
        status=ticket.status.value,
        allowed_statuses=[
            status.value
            for status in allowed_statuses(
                ticket, requester=current_user.role == ROLE_REQUESTER
            )
        ],
        assignee_id=ticket.assignee_id,
        assignee_name=ticket.assignee.full_name if ticket.assignee else None,
        updated_at=iso_utc(ticket.updated_at) or "",
        last_activity_at=iso_utc(
            ticket.last_activity_at or ticket.updated_at
        ) or "",
        sla_state=sla["state"],
        sla_label=sla["label"],
        sla_due_at=(
            iso_utc(sla.get("due_at"))
        ),
        comments=[
            _serialize_comment(comment)
            for comment in comments
        ],
        typing_users=_active_typing_users(db, ticket.id, current_user.id),
    )


@router.post("/tickets/{ticket_id}/typing", status_code=204)
def ticket_typing(
    ticket_id: int,
    payload: TypingIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_ticket_access(ticket, current_user)
    presence = db.query(TypingPresence).filter(TypingPresence.ticket_id == ticket.id, TypingPresence.user_id == current_user.id).first()
    if payload.typing:
        if presence is None:
            presence = TypingPresence(ticket_id=ticket.id, user_id=current_user.id, user_name=current_user.full_name, expires_at=utcnow() + timedelta(seconds=5))
            db.add(presence)
        else:
            presence.user_name = current_user.full_name
            presence.expires_at = utcnow() + timedelta(seconds=5)
    elif presence is not None:
        db.delete(presence)
    db.commit()


@router.get("/attachments/{attachment_id}")
def get_attachment(
    attachment_id: int,
    download: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    attachment = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Anexo não encontrado")
    _ensure_ticket_access(attachment.ticket, current_user)
    if (
        current_user.role == ROLE_REQUESTER
        and attachment.comment is not None
        and attachment.comment.is_internal
    ):
        raise HTTPException(status_code=404, detail="Anexo não encontrado")
    path = attachment_path(attachment)
    inline = is_previewable(attachment.content_type) and not download
    disposition = "inline" if inline else "attachment"
    safe_filename = quote(attachment.original_name)
    if download:
        record_event(
            db,
            "ticket.attachment.downloaded",
            actor=current_user,
            ticket=attachment.ticket,
            summary="Download explícito de anexo registrado.",
            changes={"attachment_id": attachment.id},
            source="api",
            category="business",
            resource_type="attachment",
            resource_id=attachment.id,
        )
        db.commit()
    return FileResponse(
        path,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": (
                f"{disposition}; filename*=UTF-8''{safe_filename}"
            ),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "private, max-age=300",
        },
    )


# --------------------------------------------------------------------------
# Comentários
# --------------------------------------------------------------------------
@router.post("/tickets/{ticket_id}/comments", response_model=CommentOut)
def add_comment(
    ticket_id: int,
    payload: CommentIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_ticket_access(ticket, current_user)
    if payload.is_internal and not _is_staff(current_user):
        raise HTTPException(status_code=403, detail="Nota interna restrita à equipe")
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="Escreva uma mensagem")
    comment = Comment(
        ticket_id=ticket.id,
        author_id=current_user.id,
        content=content,
        is_system=False,
        is_internal=payload.is_internal,
    )
    db.add(comment)
    db.flush()
    previous_status = ticket.status
    _apply_message_activity(
        db,
        ticket,
        current_user,
        is_internal=payload.is_internal,
    )
    record_event(
        db,
        (
            "ticket.comment.internal_added"
            if payload.is_internal
            else "ticket.comment.public_added"
        ),
        actor=current_user,
        ticket=ticket,
        summary=(
            "Nota interna adicionada à demanda."
            if payload.is_internal
            else "Mensagem pública adicionada à demanda."
        ),
        changes={
            "comment_id": comment.id,
            "has_note": True,
            "attachment_count": 0,
        },
        source="api",
    )
    if ticket.status != previous_status:
        record_event(
            db,
            "ticket.status.auto_changed",
            actor=current_user,
            ticket=ticket,
            summary="Status da demanda atualizado automaticamente.",
            changes={
                "before": {"status": previous_status.value},
                "after": {"status": ticket.status.value},
            },
            source="api",
        )
    target = _notification_target(ticket, current_user)
    if target and not payload.is_internal:
        notify(db, target, ticket, "Nova mensagem", "Há uma nova mensagem pública na demanda.")
        enqueue_ticket_update(
            db,
            target.email,
            target.full_name,
            ticket.id,
            ticket.subject,
            "Há uma nova mensagem pública disponível na demanda.",
        )
    db.commit()
    db.refresh(comment)
    return _serialize_comment(comment)


@router.post("/tickets/{ticket_id}/messages", response_model=CommentOut)
def add_message_with_attachments(
    ticket_id: int,
    content: str = Form(""),
    is_internal: bool = Form(False),
    attachments: list[UploadFile] = File(default=[]),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_ticket_access(ticket, current_user)
    if is_internal and not _is_staff(current_user):
        raise HTTPException(status_code=403, detail="Nota interna restrita à equipe")
    content = content.strip()
    valid_files = [item for item in attachments if item.filename]
    if not content and not valid_files:
        raise HTTPException(status_code=422, detail="Escreva uma mensagem ou anexe um arquivo")
    if len(content) > 5000:
        raise HTTPException(status_code=422, detail="Mensagem muito longa")

    comment = Comment(
        ticket_id=ticket.id,
        author_id=current_user.id,
        content=content or "Anexo enviado.",
        is_system=False,
        is_internal=is_internal,
    )
    saved_attachments: list[Attachment] = []
    try:
        db.add(comment)
        db.flush()
        previous_status = ticket.status
        saved_attachments = save_uploads(
            db,
            ticket,
            current_user,
            valid_files,
            comment=comment,
        )
        _apply_message_activity(
            db,
            ticket,
            current_user,
            is_internal=is_internal,
        )
        record_event(
            db,
            (
                "ticket.comment.internal_added"
                if is_internal
                else "ticket.comment.public_added"
            ),
            actor=current_user,
            ticket=ticket,
            summary=(
                "Nota interna adicionada à demanda."
                if is_internal
                else "Mensagem pública adicionada à demanda."
            ),
            changes={
                "comment_id": comment.id,
                "has_note": bool(content),
                "attachment_count": len(saved_attachments),
            },
            source="api",
        )
        if ticket.status != previous_status:
            record_event(
                db,
                "ticket.status.auto_changed",
                actor=current_user,
                ticket=ticket,
                summary="Status da demanda atualizado automaticamente.",
                changes={
                    "before": {"status": previous_status.value},
                    "after": {"status": ticket.status.value},
                },
                source="api",
            )
        update_text = "Há uma nova mensagem pública disponível na demanda."
        if valid_files:
            update_text += f" A mensagem possui {len(valid_files)} anexo(s)."
        target = _notification_target(ticket, current_user)
        if target and not is_internal:
            notify(db, target, ticket, "Nova mensagem", update_text)
            enqueue_ticket_update(
                db,
                target.email,
                target.full_name,
                ticket.id,
                ticket.subject,
                update_text,
            )
        db.commit()
        db.refresh(comment)
    except Exception:
        db.rollback()
        delete_saved_uploads(saved_attachments)
        raise

    return _serialize_comment(comment)


# --------------------------------------------------------------------------
# Mudança de status
# --------------------------------------------------------------------------
@router.patch("/tickets/{ticket_id}/status", response_model=CommentOut)
def change_status(
    ticket_id: int,
    payload: StatusIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_ticket_access(ticket, current_user)
    requester_action = current_user.role == ROLE_REQUESTER
    if not requester_action and not _is_staff(current_user):
        raise HTTPException(status_code=403, detail="Acesso negado")
    previous_status = ticket.status
    try:
        content = transition_ticket(
            ticket,
            payload.status,
            requester=requester_action,
            note=payload.note,
        )
        if not requester_action:
            mark_first_response(ticket)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    event = Comment(
        ticket_id=ticket.id,
        author_id=current_user.id,
        content=content,
        is_system=True,
        is_internal=False,
    )
    db.add(event)
    db.flush()
    record_event(
        db,
        "ticket.status.changed",
        actor=current_user,
        ticket=ticket,
        summary="Status da demanda alterado manualmente.",
        changes={
            "before": {"status": previous_status.value},
            "after": {"status": ticket.status.value},
            "has_note": bool((payload.note or "").strip()),
            "comment_id": event.id,
        },
        source="api",
    )
    target = _notification_target(ticket, current_user)
    if target:
        if payload.status == TicketStatus.CONCLUIDO and not requester_action:
            enqueue_ticket_completed(
                db,
                target.email,
                target.full_name,
                ticket.id,
                ticket.subject,
            )
        else:
            enqueue_ticket_update(
                db,
                target.email,
                target.full_name,
                ticket.id,
                ticket.subject,
                f"Status da demanda atualizado para {ticket.status.value}.",
            )
    db.commit()
    db.refresh(event)
    return _serialize_comment(event)


@router.post("/tickets/{ticket_id}/satisfaction", status_code=201)
def rate_ticket(ticket_id: int, payload: SatisfactionIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ticket = _get_ticket_or_404(db, ticket_id)
    if ticket.requester_id != current_user.id:
        raise HTTPException(status_code=403, detail="Apenas o solicitante pode avaliar")
    if ticket.status not in RESOLUTION_STATUSES:
        raise HTTPException(status_code=409, detail="A demanda ainda não foi finalizada")
    if db.query(SatisfactionRating).filter(SatisfactionRating.ticket_id == ticket.id).first():
        raise HTTPException(status_code=409, detail="Esta demanda já foi avaliada")
    rating = SatisfactionRating(ticket_id=ticket.id, requester_id=current_user.id, score=payload.score, comment=(payload.comment or "").strip() or None)
    db.add(rating)
    record_event(db, "ticket.satisfaction.created", actor=current_user, ticket=ticket, summary="Avaliação de atendimento registrada.", changes={"score": payload.score}, source="api")
    db.commit()
    return {"ok": True, "score": payload.score}


@router.get("/notifications")
def list_notifications(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = db.query(InAppNotification).filter(InAppNotification.user_id == current_user.id).order_by(InAppNotification.created_at.desc()).limit(50).all()
    return [{"id": item.id, "ticket_id": item.ticket_id, "title": item.title, "message": item.message, "read": item.read_at is not None, "created_at": iso_utc(item.created_at)} for item in items]


@router.post("/notifications/{notification_id}/read", status_code=204)
def read_notification(notification_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(InAppNotification).filter(InAppNotification.id == notification_id, InAppNotification.user_id == current_user.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Notificação não encontrada")
    item.read_at = utcnow()
    db.commit()


@router.get("/catalog/categories")
def list_categories(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [{"id": item.id, "name": item.name} for item in db.query(Category).filter(Category.is_active.is_(True)).order_by(Category.name).all()]


@router.post("/catalog/categories", status_code=201)
def create_category(payload: CatalogItemIn, current_user: User = Depends(require_roles(ROLE_ADMIN)), db: Session = Depends(get_db)):
    name = payload.name.strip()
    if db.query(Category).filter(func.lower(Category.name) == name.lower()).first():
        raise HTTPException(status_code=409, detail="Categoria já cadastrada")
    item = Category(name=name)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "name": item.name}


@router.get("/canned-responses")
def list_canned_responses(current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)), db: Session = Depends(get_db)):
    return [{"id": item.id, "title": item.title, "content": item.content} for item in db.query(CannedResponse).filter(CannedResponse.is_active.is_(True)).order_by(CannedResponse.title).all()]


@router.post("/canned-responses", status_code=201)
def create_canned_response(payload: CannedResponseIn, current_user: User = Depends(require_roles(ROLE_ADMIN)), db: Session = Depends(get_db)):
    item = CannedResponse(title=payload.title.strip(), content=payload.content.strip(), created_by_id=current_user.id)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "title": item.title, "content": item.content}


@router.patch("/bulk/tickets/status")
def bulk_ticket_status(payload: BulkStatusIn, current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)), db: Session = Depends(get_db)):
    ids = sorted(set(payload.ticket_ids))
    tickets = db.query(Ticket).filter(Ticket.id.in_(ids)).all()
    if len(tickets) != len(ids):
        raise HTTPException(status_code=404, detail="Uma ou mais demandas não foram encontradas")
    try:
        for ticket in tickets:
            transition_ticket(ticket, payload.status, requester=False, note=payload.note)
            db.add(Comment(ticket_id=ticket.id, author_id=current_user.id, content=f'Status alterado em lote para "{payload.status.value}".', is_system=True, is_internal=False))
        db.commit()
    except WorkflowError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"updated": ids, "status": payload.status.value}


# --------------------------------------------------------------------------
# Atribuição de técnico
# --------------------------------------------------------------------------
@router.patch("/tickets/{ticket_id}/assignee", response_model=CommentOut)
def change_assignee(
    ticket_id: int,
    payload: AssigneeIn,
    current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    previous_assignee_id = ticket.assignee_id
    assigned_tech: User | None = None

    if payload.assignee_id is None:
        ticket.assignee_id = None
        content = "Responsável removido do chamado."
    else:
        assigned_tech = (
            db.query(User)
            .filter(
                User.id == payload.assignee_id,
                User.role.in_([ROLE_ADMIN, ROLE_TECHNICIAN]),
                User.is_active.is_(True),
            )
            .first()
        )
        if not assigned_tech:
            raise HTTPException(status_code=400, detail="Técnico inválido")
        ticket.assignee_id = assigned_tech.id
        content = f"Chamado atribuído a {assigned_tech.full_name}."

    event = Comment(
        ticket_id=ticket.id,
        author_id=current_user.id,
        content=content,
        is_system=True,
        is_internal=False,
    )
    db.add(event)
    db.flush()
    touch_ticket(ticket)
    record_event(
        db,
        "ticket.assignee.changed",
        actor=current_user,
        ticket=ticket,
        summary="Responsável pela demanda alterado.",
        changes={
            "before": {"assignee_id": previous_assignee_id},
            "after": {"assignee_id": ticket.assignee_id},
            "comment_id": event.id,
        },
        source="api",
    )
    enqueue_ticket_update(
        db,
        ticket.requester.email,
        ticket.requester.full_name,
        ticket.id,
        ticket.subject,
        "A responsabilidade pelo atendimento foi atualizada.",
    )
    if (
        assigned_tech is not None
        and assigned_tech.id != previous_assignee_id
        and assigned_tech.id != current_user.id
    ):
        enqueue_ticket_update(
            db,
            assigned_tech.email,
            assigned_tech.full_name,
            ticket.id,
            ticket.subject,
            "Você foi definido como responsável por esta demanda.",
        )
    db.commit()
    db.refresh(event)
    return _serialize_comment(event)


# --------------------------------------------------------------------------
# Metadados operacionais
# --------------------------------------------------------------------------
@router.patch("/tickets/{ticket_id}/metadata", response_model=CommentOut)
def change_ticket_metadata(
    ticket_id: int,
    payload: TicketMetadataIn,
    current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    changed: list[str] = []
    audit_before: dict[str, object] = {}
    audit_after: dict[str, object] = {}
    fields = payload.model_fields_set

    if "priority" in fields:
        if payload.priority is None:
            raise HTTPException(status_code=422, detail="Prioridade obrigatória")
        if payload.priority != ticket.priority:
            old_priority = ticket.priority
            audit_before["priority"] = old_priority.value
            ticket.priority = payload.priority
            audit_after["priority"] = payload.priority.value
            initialize_sla(ticket, reset=True)
            changed.append(
                f'prioridade de "{old_priority.value}" para '
                f'"{payload.priority.value}"'
            )

    if "project_code" in fields:
        if payload.project_code is None:
            if ticket.project_code is not None:
                audit_before["project_code"] = ticket.project_code
                ticket.project_code = None
                ticket.project_name = None
                audit_after["project_code"] = None
                changed.append("projeto removido")
        else:
            try:
                projeto = find_projeto(payload.project_code)
            except ProjetosUnavailableError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="A lista de projetos está temporariamente indisponível.",
                ) from exc
            if projeto is None:
                raise HTTPException(status_code=422, detail="Projeto inválido")
            if (
                ticket.project_code != projeto["cod_projeto"]
                or ticket.project_name != projeto["cod_projeto_alfa"]
            ):
                old_project = ticket.project_name or "sem projeto"
                audit_before["project_code"] = ticket.project_code
                ticket.project_code = projeto["cod_projeto"]
                ticket.project_name = projeto["cod_projeto_alfa"]
                audit_after["project_code"] = ticket.project_code
                changed.append(
                    f'projeto de "{old_project}" para '
                    f'"{ticket.project_name}"'
                )

    for field_name, label in (("category", "categoria"), ("hub", "hub")):
        if field_name not in fields:
            continue
        value = getattr(payload, field_name)
        clean_value = value.strip() if value else None
        old_value = getattr(ticket, field_name)
        if clean_value != old_value:
            audit_before[field_name] = old_value
            setattr(ticket, field_name, clean_value)
            audit_after[field_name] = clean_value
            changed.append(
                f'{label} de "{old_value or "não informado"}" para '
                f'"{clean_value or "não informado"}"'
            )

    if not changed:
        raise HTTPException(status_code=409, detail="Nenhuma alteração informada")

    content = "Metadados atualizados: " + "; ".join(changed) + "."
    event = Comment(
        ticket_id=ticket.id,
        author_id=current_user.id,
        content=content,
        is_system=True,
        is_internal=False,
    )
    db.add(event)
    db.flush()
    touch_ticket(ticket)
    record_event(
        db,
        "ticket.metadata.changed",
        actor=current_user,
        ticket=ticket,
        summary="Dados operacionais da demanda alterados.",
        changes={
            "before": audit_before,
            "after": audit_after,
            "comment_id": event.id,
        },
        source="api",
    )
    db.commit()
    db.refresh(event)
    return _serialize_comment(event)
