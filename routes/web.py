"""
routes/web.py
-------------
Rotas que renderizam as páginas HTML (Jinja2): login, dashboard e detalhe
do ticket. As ações de escrita (comentar, mudar status) ficam em routes/api.py.
"""
from __future__ import annotations

import json

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import (
    ROLE_ADMIN,
    ROLE_REQUESTER,
    ROLE_TECHNICIAN,
    get_current_user,
    login_user,
    logout_user,
    require_roles,
)
from attachment_service import save_uploads
from database import Ticket, TicketPriority, TicketStatus, User, get_db
from ldap_auth import authenticate, list_active_ldap_users
from notification_service import send_ticket_received

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.filters["from_json"] = json.loads


# --------------------------------------------------------------------------
# Autenticação
# --------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    # 1) Autentica no AD (mock em DEV_MODE)
    ad_user = authenticate(email, password)
    if not ad_user:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Credenciais inválidas ou usuário inativo."},
            status_code=401,
        )
    # 2) Confirma que o usuário existe na base local (sincronizado do AD)
    user = db.query(User).filter(User.email == ad_user["email"], User.is_active.is_(True)).first()
    if not user:
        # Sincroniza on-the-fly caso ainda não exista localmente
        user = User(
            email=ad_user["email"],
            full_name=ad_user["full_name"],
            department=ad_user.get("department"),
            hubs=json.dumps(ad_user.get("hubs", []), ensure_ascii=False),
            role=ROLE_REQUESTER,
            is_technician=False,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Dados organizacionais vêm do LDAP; permissões permanecem no sistema.
        user.full_name = ad_user["full_name"]
        user.department = ad_user.get("department")
        user.hubs = json.dumps(ad_user.get("hubs", []), ensure_ascii=False)
        db.commit()

    login_user(request, user)
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)


# --------------------------------------------------------------------------
# Cadastro de demanda pelo portal
# --------------------------------------------------------------------------
@router.get("/demandas/nova", response_class=HTMLResponse)
def new_demand_page(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
        "new_demand.html",
        {"current_user": current_user, "error": None},
    )


@router.post("/demandas/nova", response_class=HTMLResponse)
def create_demand(
    request: Request,
    background_tasks: BackgroundTasks,
    subject: str = Form(...),
    body: str = Form(...),
    priority: str = Form(TicketPriority.MEDIA.value),
    attachments: list[UploadFile] = File(default=[]),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    subject = subject.strip()
    body = body.strip()
    if len(subject) < 5 or len(subject) > 500 or len(body) < 20:
        return templates.TemplateResponse(
            request,
            "new_demand.html",
            {
                "current_user": current_user,
                "error": (
                    "Informe um título com pelo menos 5 caracteres e uma "
                    "descrição com pelo menos 20 caracteres."
                ),
                "form": {"subject": subject, "body": body, "priority": priority},
            },
            status_code=422,
        )
    try:
        ticket_priority = TicketPriority(priority)
    except ValueError:
        ticket_priority = TicketPriority.MEDIA

    ticket = Ticket(
        subject=subject,
        body=body,
        status=TicketStatus.ABERTO,
        priority=ticket_priority,
        requester_id=current_user.id,
    )
    try:
        db.add(ticket)
        db.flush()
        save_uploads(db, ticket, current_user, attachments)
        db.commit()
        db.refresh(ticket)
    except HTTPException as exc:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "new_demand.html",
            {
                "current_user": current_user,
                "error": exc.detail,
                "form": {"subject": subject, "body": body, "priority": priority},
            },
            status_code=exc.status_code,
        )
    background_tasks.add_task(
        send_ticket_received,
        current_user.email,
        current_user.full_name,
        ticket.id,
        ticket.subject,
        ticket.priority.value,
    )
    return RedirectResponse(
        url=f"/tickets/{ticket.id}?criada=1",
        status_code=303,
    )


@router.get("/minhas-demandas", response_class=HTMLResponse)
def my_demands(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tickets = (
        db.query(Ticket)
        .filter(Ticket.requester_id == current_user.id)
        .order_by(Ticket.updated_at.desc())
        .all()
    )
    counts = {
        "total": len(tickets),
        "abertos": sum(t.status == TicketStatus.ABERTO for t in tickets),
        "andamento": sum(t.status == TicketStatus.EM_ANDAMENTO for t in tickets),
        "concluidos": sum(t.status == TicketStatus.CONCLUIDO for t in tickets),
    }
    return templates.TemplateResponse(
        request,
        "my_demands.html",
        {
            "current_user": current_user,
            "tickets": tickets,
            "counts": counts,
        },
    )


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    status: str | None = None,
    priority: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Ticket)
    metrics_query = db.query(Ticket)
    if current_user.role == ROLE_REQUESTER:
        query = query.filter(Ticket.requester_id == current_user.id)
        metrics_query = metrics_query.filter(Ticket.requester_id == current_user.id)
    if status:
        query = query.filter(Ticket.status == status)
    if priority:
        query = query.filter(Ticket.priority == priority)
    tickets = query.order_by(Ticket.created_at.desc()).all()

    # Métricas por status (contagem global, independente do filtro)
    counts = dict(
        metrics_query.with_entities(Ticket.status, func.count(Ticket.id))
        .group_by(Ticket.status)
        .all()
    )
    metrics = {
        "abertos": counts.get(TicketStatus.ABERTO, 0),
        "em_andamento": counts.get(TicketStatus.EM_ANDAMENTO, 0),
        "concluidos": counts.get(TicketStatus.CONCLUIDO, 0),
        "total": sum(counts.values()),
    }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current_user": current_user,
            "tickets": tickets,
            "metrics": metrics,
            "active_status": status,
            "active_priority": priority,
        },
    )


# --------------------------------------------------------------------------
# Detalhe do ticket
# --------------------------------------------------------------------------
@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(
    ticket_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if (
        ticket
        and current_user.role == ROLE_REQUESTER
        and ticket.requester_id != current_user.id
    ):
        ticket = None
    if not ticket:
        return templates.TemplateResponse(
            request, "404.html", {"current_user": current_user}, status_code=404
        )
    technicians = (
        db.query(User)
        .filter(
            User.role.in_([ROLE_TECHNICIAN, ROLE_ADMIN]),
            User.is_active.is_(True),
        )
        .all()
    )
    return templates.TemplateResponse(
        request,
        "ticket_detail.html",
        {
            "current_user": current_user,
            "ticket": ticket,
            "technicians": technicians,
        },
    )


@router.get("/admin/usuarios", response_class=HTMLResponse)
def admin_users(
    request: Request,
    current_user: User = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    users = db.query(User).order_by(User.full_name.asc()).all()
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {"current_user": current_user, "users": users},
    )


@router.post("/admin/usuarios/{user_id}/perfil")
def change_user_role(
    user_id: int,
    role: str = Form(...),
    current_user: User = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    allowed = {ROLE_ADMIN, ROLE_TECHNICIAN, ROLE_REQUESTER}
    if role not in allowed:
        return RedirectResponse("/admin/usuarios?erro=perfil-invalido", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/admin/usuarios?erro=usuario-invalido", status_code=303)
    if user.id == current_user.id and role != ROLE_ADMIN:
        return RedirectResponse("/admin/usuarios?erro=auto-rebaixamento", status_code=303)
    user.role = role
    user.is_technician = role in {ROLE_ADMIN, ROLE_TECHNICIAN}
    db.commit()
    return RedirectResponse("/admin/usuarios?salvo=1", status_code=303)


@router.post("/admin/usuarios/sincronizar")
def sync_ldap_users(
    current_user: User = Depends(require_roles(ROLE_ADMIN)),
    db: Session = Depends(get_db),
):
    ldap_users = list_active_ldap_users()
    existing_by_email = {
        user.email.lower(): user
        for user in db.query(User).all()
    }
    created = 0
    updated = 0
    for ldap_user in ldap_users:
        email = ldap_user["email"].lower()
        user = existing_by_email.get(email)
        if user is None:
            user = User(
                email=email,
                full_name=ldap_user["full_name"],
                department=ldap_user.get("department"),
                hubs=json.dumps(ldap_user.get("hubs", []), ensure_ascii=False),
                role=ROLE_REQUESTER,
                is_technician=False,
                is_active=True,
            )
            db.add(user)
            existing_by_email[email] = user
            created += 1
        else:
            user.full_name = ldap_user["full_name"]
            user.department = ldap_user.get("department")
            user.hubs = json.dumps(ldap_user.get("hubs", []), ensure_ascii=False)
            user.is_active = True
            updated += 1
    db.commit()
    return RedirectResponse(
        f"/admin/usuarios?sincronizados={len(ldap_users)}"
        f"&criados={created}&atualizados={updated}",
        status_code=303,
    )
