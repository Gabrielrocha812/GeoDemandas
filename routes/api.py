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

import time
from threading import Lock
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import (
    ROLE_ADMIN,
    ROLE_REQUESTER,
    ROLE_TECHNICIAN,
    get_current_user,
    require_roles,
)
from attachment_service import (
    attachment_path,
    is_preview_image,
    is_previewable,
    save_uploads,
)
from database import Attachment, Comment, Ticket, TicketStatus, User, get_db
from notification_service import send_ticket_completed, send_ticket_update

router = APIRouter(prefix="/api", tags=["api"])
_typing_lock = Lock()
_typing_users: dict[int, dict[int, tuple[str, float]]] = {}


# --------------------------------------------------------------------------
# Schemas de entrada/saída
# --------------------------------------------------------------------------
class CommentIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)


class StatusIn(BaseModel):
    status: TicketStatus


class AssigneeIn(BaseModel):
    assignee_id: int | None = None


class TypingIn(BaseModel):
    typing: bool = True


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
    created_at: str
    attachments: list[AttachmentOut] = Field(default_factory=list)


class TicketStateOut(BaseModel):
    status: str
    assignee_id: int | None
    assignee_name: str | None
    updated_at: str
    comments: list[CommentOut]
    typing_users: list[str]


def _serialize_comment(c: Comment) -> CommentOut:
    return CommentOut(
        id=c.id,
        content=c.content,
        author_name=c.author.full_name,
        is_system=c.is_system,
        created_at=c.created_at.strftime("%d/%m/%Y %H:%M"),
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
    if current_user.role == ROLE_REQUESTER and ticket.requester_id != current_user.id:
        raise HTTPException(status_code=404, detail="Ticket não encontrado")


def _active_typing_users(ticket_id: int, current_user_id: int) -> list[str]:
    now = time.monotonic()
    with _typing_lock:
        users = _typing_users.get(ticket_id, {})
        expired = [user_id for user_id, (_, until) in users.items() if until <= now]
        for user_id in expired:
            users.pop(user_id, None)
        if not users:
            _typing_users.pop(ticket_id, None)
            return []
        return [
            name
            for user_id, (name, _) in users.items()
            if user_id != current_user_id
        ]


@router.get("/tickets/{ticket_id}/state", response_model=TicketStateOut)
def ticket_state(
    ticket_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_ticket_access(ticket, current_user)
    return TicketStateOut(
        status=ticket.status.value,
        assignee_id=ticket.assignee_id,
        assignee_name=ticket.assignee.full_name if ticket.assignee else None,
        updated_at=ticket.updated_at.isoformat(),
        comments=[_serialize_comment(comment) for comment in ticket.comments],
        typing_users=_active_typing_users(ticket.id, current_user.id),
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
    with _typing_lock:
        users = _typing_users.setdefault(ticket.id, {})
        if payload.typing:
            users[current_user.id] = (
                current_user.full_name,
                time.monotonic() + 5,
            )
        else:
            users.pop(current_user.id, None)
            if not users:
                _typing_users.pop(ticket.id, None)


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
    path = attachment_path(attachment)
    inline = is_previewable(attachment.content_type) and not download
    disposition = "inline" if inline else "attachment"
    safe_filename = quote(attachment.original_name)
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
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_ticket_access(ticket, current_user)
    comment = Comment(
        ticket_id=ticket.id,
        author_id=current_user.id,
        content=payload.content.strip(),
        is_system=False,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    if current_user.id == ticket.requester_id and ticket.assignee:
        background_tasks.add_task(
            send_ticket_update,
            ticket.assignee.email,
            ticket.assignee.full_name,
            ticket.id,
            ticket.subject,
            f"Nova mensagem do solicitante {current_user.full_name}: {comment.content[:500]}",
        )
    elif current_user.id != ticket.requester_id:
        background_tasks.add_task(
            send_ticket_update,
            ticket.requester.email,
            ticket.requester.full_name,
            ticket.id,
            ticket.subject,
            f"Novo comentário de {current_user.full_name}: {comment.content[:500]}",
        )
    return _serialize_comment(comment)


@router.post("/tickets/{ticket_id}/messages", response_model=CommentOut)
def add_message_with_attachments(
    ticket_id: int,
    background_tasks: BackgroundTasks,
    content: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_ticket_access(ticket, current_user)
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
    )
    try:
        db.add(comment)
        db.flush()
        save_uploads(db, ticket, current_user, valid_files, comment=comment)
        db.commit()
        db.refresh(comment)
    except Exception:
        db.rollback()
        raise

    update_text = (
        f"Nova mensagem de {current_user.full_name}: {comment.content[:500]}"
    )
    if valid_files:
        update_text += f" ({len(valid_files)} anexo(s))"
    if current_user.id == ticket.requester_id and ticket.assignee:
        background_tasks.add_task(
            send_ticket_update,
            ticket.assignee.email,
            ticket.assignee.full_name,
            ticket.id,
            ticket.subject,
            update_text,
        )
    elif current_user.id != ticket.requester_id:
        background_tasks.add_task(
            send_ticket_update,
            ticket.requester.email,
            ticket.requester.full_name,
            ticket.id,
            ticket.subject,
            update_text,
        )
    return _serialize_comment(comment)


# --------------------------------------------------------------------------
# Mudança de status
# --------------------------------------------------------------------------
@router.patch("/tickets/{ticket_id}/status", response_model=CommentOut)
def change_status(
    ticket_id: int,
    payload: StatusIn,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    old = ticket.status
    ticket.status = payload.status

    # Registra o evento na timeline como comentário de sistema
    event = Comment(
        ticket_id=ticket.id,
        author_id=current_user.id,
        content=f"Status alterado de \"{old.value}\" para \"{payload.status.value}\".",
        is_system=True,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    notification = (
        send_ticket_completed
        if payload.status == TicketStatus.CONCLUIDO
        else send_ticket_update
    )
    if payload.status == TicketStatus.CONCLUIDO:
        background_tasks.add_task(
            notification,
            ticket.requester.email,
            ticket.requester.full_name,
            ticket.id,
            ticket.subject,
        )
    else:
        background_tasks.add_task(
            notification,
            ticket.requester.email,
            ticket.requester.full_name,
            ticket.id,
            ticket.subject,
            f'Status alterado para "{payload.status.value}".',
        )
    return _serialize_comment(event)


# --------------------------------------------------------------------------
# Atribuição de técnico
# --------------------------------------------------------------------------
@router.patch("/tickets/{ticket_id}/assignee", response_model=CommentOut)
def change_assignee(
    ticket_id: int,
    payload: AssigneeIn,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)),
    db: Session = Depends(get_db),
):
    ticket = _get_ticket_or_404(db, ticket_id)

    if payload.assignee_id is None:
        ticket.assignee_id = None
        content = "Responsável removido do chamado."
    else:
        tech = (
            db.query(User)
            .filter(
                User.id == payload.assignee_id,
                User.role.in_([ROLE_ADMIN, ROLE_TECHNICIAN]),
                User.is_active.is_(True),
            )
            .first()
        )
        if not tech:
            raise HTTPException(status_code=400, detail="Técnico inválido")
        ticket.assignee_id = tech.id
        content = f"Chamado atribuído a {tech.full_name}."

    event = Comment(
        ticket_id=ticket.id,
        author_id=current_user.id,
        content=content,
        is_system=True,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    background_tasks.add_task(
        send_ticket_update,
        ticket.requester.email,
        ticket.requester.full_name,
        ticket.id,
        ticket.subject,
        content,
    )
    return _serialize_comment(event)
