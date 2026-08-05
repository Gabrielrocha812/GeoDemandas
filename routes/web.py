"""
routes/web.py
-------------
Rotas que renderizam as páginas HTML (Jinja2): login, dashboard e detalhe
do ticket. As ações de escrita (comentar, mudar status) ficam em routes/api.py.
"""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import Session, joinedload

from audit_service import record_event
from auth import (
    ROLE_ADMIN,
    ROLE_REQUESTER,
    ROLE_TECHNICIAN,
    get_current_user,
    login_user,
    logout_user,
    require_roles,
)
from attachment_service import delete_saved_uploads, save_uploads
from config import settings
from database import Ticket, TicketPriority, TicketStatus, User, get_db, utcnow
from ldap_auth import LDAPOperationalError, authenticate, list_active_ldap_users
from outbox_service import enqueue_ticket_received
from projeto_service import ProjetosUnavailableError, find_projeto, list_projetos
from time_utils import format_local_datetime, iso_utc
from workflow_service import (
    RESOLUTION_STATUSES,
    allowed_statuses,
    initialize_sla,
    sla_snapshot,
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.filters["from_json"] = json.loads
templates.env.filters["local_datetime"] = format_local_datetime
templates.env.filters["iso_utc"] = iso_utc

PROJETOS_INDISPONIVEIS = (
    "Não foi possível carregar a lista de projetos agora. "
    "Você pode cadastrar a demanda mesmo assim e vincular o projeto depois."
)


def _load_projetos() -> tuple[list[dict], str | None]:
    """Projetos da API corporativa + aviso quando a integração está fora."""
    try:
        return list_projetos(), None
    except ProjetosUnavailableError:
        return [], PROJETOS_INDISPONIVEIS


def _parse_status(value: str | None) -> TicketStatus | None:
    if not value:
        return None
    try:
        return TicketStatus(value)
    except ValueError:
        return None


def _parse_priority(value: str | None) -> TicketPriority | None:
    if not value:
        return None
    try:
        return TicketPriority(value)
    except ValueError:
        return None


def _user_hubs(user: User) -> list[str]:
    try:
        hubs = json.loads(user.hubs or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(hubs, list):
        return []
    return [str(hub).strip() for hub in hubs if str(hub).strip()]


def _known_hubs(db: Session, ticket: Ticket) -> list[str]:
    hubs = {
        hub
        for raw_hubs in db.query(User.hubs).filter(User.is_active.is_(True)).all()
        for hub in _safe_hub_list(raw_hubs[0])
    }
    if ticket.hub:
        hubs.add(ticket.hub)
    return sorted(hubs, key=str.casefold)


def _safe_hub_list(raw_hubs: str | None) -> list[str]:
    try:
        values = json.loads(raw_hubs or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


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
    try:
        ad_user = authenticate(email, password)
    except LDAPOperationalError:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": (
                    "O diretório corporativo está temporariamente indisponível. "
                    "Tente novamente em alguns instantes."
                )
            },
            status_code=503,
        )
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

    initial_admin_emails = {
        configured_email.strip().lower()
        for configured_email in settings.INITIAL_ADMIN_EMAILS.split(",")
        if configured_email.strip()
    }
    if user.email.lower() in initial_admin_emails and user.role != ROLE_ADMIN:
        user.role = ROLE_ADMIN
        user.is_technician = True
        db.commit()
        db.refresh(user)

    login_user(request, user)
    destination = "/minhas-demandas" if user.role == ROLE_REQUESTER else "/"
    return RedirectResponse(url=destination, status_code=303)


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
    projetos, projetos_error = _load_projetos()
    return templates.TemplateResponse(
        request,
        "new_demand.html",
        {
            "current_user": current_user,
            "error": None,
            "projetos": projetos,
            "projetos_error": projetos_error,
        },
    )


@router.post("/demandas/nova", response_class=HTMLResponse)
def create_demand(
    request: Request,
    subject: str = Form(...),
    body: str = Form(...),
    priority: str = Form(TicketPriority.MEDIA.value),
    project_code: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    subject = subject.strip()
    body = body.strip()
    project_code = project_code.strip()
    projetos, projetos_error = _load_projetos()

    def render_error(message: str, status_code: int = 422):
        return templates.TemplateResponse(
            request,
            "new_demand.html",
            {
                "current_user": current_user,
                "error": message,
                "projetos": projetos,
                "projetos_error": projetos_error,
                "form": {
                    "subject": subject,
                    "body": body,
                    "priority": priority,
                    "project_code": project_code,
                },
            },
            status_code=status_code,
        )

    if len(subject) < 5 or len(subject) > 500 or len(body) < 20:
        return render_error(
            "Informe um título com pelo menos 5 caracteres e uma "
            "descrição com pelo menos 20 caracteres."
        )

    # Só exigimos o projeto quando conseguimos carregar a lista da API — caso
    # contrário o cadastro ficaria bloqueado por uma indisponibilidade externa.
    projeto = None
    if projetos:
        projeto = find_projeto(project_code)
        if projeto is None:
            return render_error(
                "Selecione o projeto ao qual esta demanda pertence."
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
        project_code=projeto["cod_projeto"] if projeto else None,
        project_name=projeto["cod_projeto_alfa"] if projeto else None,
        hub=(_user_hubs(current_user) or [None])[0],
        source_channel="portal",
    )
    initialize_sla(ticket)
    saved_attachments = []
    try:
        db.add(ticket)
        db.flush()
        saved_attachments = save_uploads(
            db,
            ticket,
            current_user,
            attachments,
        )
        record_event(
            db,
            "ticket.created",
            actor=current_user,
            ticket=ticket,
            summary="Demanda criada pelo portal.",
            changes={
                "status": ticket.status,
                "priority": ticket.priority,
                "project_code": ticket.project_code,
                "hub": ticket.hub,
                "source_channel": ticket.source_channel,
                "attachment_count": len(saved_attachments),
            },
            source="web",
        )
        enqueue_ticket_received(
            db,
            current_user.email,
            current_user.full_name,
            ticket.id,
            ticket.subject,
            ticket.priority.value,
            dedupe_key=f"ticket-received:{ticket.id}",
        )
        db.commit()
        db.refresh(ticket)
    except HTTPException as exc:
        db.rollback()
        delete_saved_uploads(saved_attachments)
        return render_error(exc.detail, status_code=exc.status_code)
    except Exception:
        db.rollback()
        delete_saved_uploads(saved_attachments)
        raise
    return RedirectResponse(
        url=f"/tickets/{ticket.id}?criada=1",
        status_code=303,
    )


@router.get("/minhas-demandas", response_class=HTMLResponse)
def my_demands(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    search_query = (q or "").strip()[:200]
    active_status = _parse_status(status)
    query = db.query(Ticket).filter(Ticket.requester_id == current_user.id)
    if active_status:
        query = query.filter(Ticket.status == active_status)
    if search_query:
        pattern = f"%{search_query}%"
        query = query.filter(
            or_(
                cast(Ticket.id, String).ilike(pattern),
                Ticket.subject.ilike(pattern),
                Ticket.body.ilike(pattern),
                Ticket.project_name.ilike(pattern),
            )
        )

    total_items = query.count()
    page_size = max(1, min(settings.DASHBOARD_PAGE_SIZE, 100))
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = min(max(page, 1), total_pages)
    tickets = (
        query.options(joinedload(Ticket.assignee))
        .order_by(Ticket.last_activity_at.desc(), Ticket.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    raw_counts = dict(
        db.query(Ticket.status, func.count(Ticket.id))
        .filter(Ticket.requester_id == current_user.id)
        .group_by(Ticket.status)
        .all()
    )
    counts = {
        "total": sum(raw_counts.values()),
        "abertos": sum(
            raw_counts.get(item, 0)
            for item in {
                TicketStatus.ABERTO,
                TicketStatus.EM_TRIAGEM,
                TicketStatus.REABERTO,
            }
        ),
        "andamento": sum(
            raw_counts.get(item, 0)
            for item in {
                TicketStatus.EM_ANDAMENTO,
                TicketStatus.AGUARDANDO_SOLICITANTE,
                TicketStatus.BLOQUEADO,
            }
        ),
        "concluidos": sum(
            raw_counts.get(item, 0) for item in RESOLUTION_STATUSES
        ),
    }
    return templates.TemplateResponse(
        request,
        "my_demands.html",
        {
            "current_user": current_user,
            "tickets": tickets,
            "counts": counts,
            "search_query": search_query,
            "active_status": active_status.value if active_status else None,
            "status_options": list(TicketStatus),
            "page": page,
            "total_pages": total_pages,
            "total_items": total_items,
            "sla_by_ticket": {
                ticket.id: sla_snapshot(ticket) for ticket in tickets
            },
        },
    )


# --------------------------------------------------------------------------
# Kanban operacional
# --------------------------------------------------------------------------
@router.get("/kanban", response_class=HTMLResponse)
def kanban(
    request: Request,
    current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)),
    db: Session = Depends(get_db),
):
    board_statuses = (
        TicketStatus.ABERTO,
        TicketStatus.EM_TRIAGEM,
        TicketStatus.EM_ANDAMENTO,
        TicketStatus.AGUARDANDO_SOLICITANTE,
        TicketStatus.BLOQUEADO,
        TicketStatus.RESOLVIDO,
        TicketStatus.REABERTO,
    )
    tickets = (
        db.query(Ticket)
        .options(joinedload(Ticket.requester), joinedload(Ticket.assignee))
        .filter(Ticket.status.in_(board_statuses))
        .order_by(Ticket.last_activity_at.desc())
        .all()
    )
    columns = [
        {
            "status": status,
            "tickets": [ticket for ticket in tickets if ticket.status == status],
        }
        for status in board_statuses
    ]
    return templates.TemplateResponse(
        request,
        "kanban.html",
        {
            "current_user": current_user,
            "columns": columns,
            "status_options": list(TicketStatus),
            "allowed_by_ticket": {
                ticket.id: allowed_statuses(ticket, requester=False)
                for ticket in tickets
            },
            "sla_by_ticket": {
                ticket.id: sla_snapshot(ticket) for ticket in tickets
            },
        },
    )


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    q: str | None = None,
    queue: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    hub: str | None = None,
    page: int = 1,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role == ROLE_REQUESTER:
        return RedirectResponse("/minhas-demandas", status_code=303)

    search_query = (q or "").strip()[:200]
    active_status = _parse_status(status)
    active_priority = _parse_priority(priority)
    active_category = (category or "").strip()[:120]
    active_hub = (hub or "").strip()[:255]
    allowed_queues = {
        "all",
        "mine",
        "unassigned",
        "waiting",
        "sla_risk",
        "overdue",
    }
    active_queue = queue if queue in allowed_queues else "all"
    now = utcnow()
    risk_limit = now + timedelta(hours=settings.SLA_RISK_WINDOW_HOURS)
    active_workflow_statuses = [
        item for item in TicketStatus if item not in RESOLUTION_STATUSES
    ]
    overdue_condition = or_(
        (
            Ticket.first_response_at.is_(None)
            & Ticket.first_response_due_at.is_not(None)
            & (Ticket.first_response_due_at <= now)
        ),
        (
            Ticket.first_response_at.is_not(None)
            & Ticket.resolution_due_at.is_not(None)
            & (Ticket.resolution_due_at <= now)
        ),
    )
    risk_condition = or_(
        (
            Ticket.first_response_at.is_(None)
            & Ticket.first_response_due_at.is_not(None)
            & (Ticket.first_response_due_at > now)
            & (Ticket.first_response_due_at <= risk_limit)
        ),
        (
            Ticket.first_response_at.is_not(None)
            & Ticket.resolution_due_at.is_not(None)
            & (Ticket.resolution_due_at > now)
            & (Ticket.resolution_due_at <= risk_limit)
        ),
    )

    query = db.query(Ticket)
    metrics_query = db.query(Ticket)
    if active_queue == "mine":
        query = query.filter(Ticket.assignee_id == current_user.id)
    elif active_queue == "unassigned":
        query = query.filter(
            Ticket.assignee_id.is_(None),
            Ticket.status.in_(active_workflow_statuses),
        )
    elif active_queue == "waiting":
        query = query.filter(
            Ticket.status == TicketStatus.AGUARDANDO_SOLICITANTE
        )
    elif active_queue == "sla_risk":
        query = query.filter(
            Ticket.status.in_(active_workflow_statuses),
            Ticket.sla_paused_at.is_(None),
            risk_condition,
        )
    elif active_queue == "overdue":
        query = query.filter(
            Ticket.status.in_(active_workflow_statuses),
            Ticket.sla_paused_at.is_(None),
            overdue_condition,
        )

    if active_status:
        query = query.filter(Ticket.status == active_status)
    if active_priority:
        query = query.filter(Ticket.priority == active_priority)
    if active_category:
        query = query.filter(Ticket.category == active_category)
    if active_hub:
        query = query.filter(Ticket.hub == active_hub)
    if search_query:
        pattern = f"%{search_query}%"
        query = query.join(User, Ticket.requester_id == User.id).filter(
            or_(
                cast(Ticket.id, String).ilike(pattern),
                Ticket.subject.ilike(pattern),
                Ticket.body.ilike(pattern),
                Ticket.project_name.ilike(pattern),
                User.full_name.ilike(pattern),
                User.email.ilike(pattern),
            )
        )

    total_items = query.count()
    page_size = max(1, min(settings.DASHBOARD_PAGE_SIZE, 100))
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = min(max(page, 1), total_pages)
    tickets = (
        query.options(joinedload(Ticket.requester), joinedload(Ticket.assignee))
        .order_by(Ticket.last_activity_at.desc(), Ticket.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # Métricas por status (contagem global, independente do filtro)
    counts = dict(
        metrics_query.with_entities(Ticket.status, func.count(Ticket.id))
        .group_by(Ticket.status)
        .all()
    )
    metrics = {
        "abertos": sum(
            counts.get(item, 0)
            for item in {
                TicketStatus.ABERTO,
                TicketStatus.EM_TRIAGEM,
                TicketStatus.REABERTO,
            }
        ),
        "em_andamento": sum(
            counts.get(item, 0)
            for item in {
                TicketStatus.EM_ANDAMENTO,
                TicketStatus.AGUARDANDO_SOLICITANTE,
                TicketStatus.BLOQUEADO,
            }
        ),
        "concluidos": sum(
            counts.get(item, 0) for item in RESOLUTION_STATUSES
        ),
        "total": sum(counts.values()),
    }

    queue_counts = {}
    if current_user.role in {ROLE_ADMIN, ROLE_TECHNICIAN}:
        queue_counts = {
            "all": db.query(Ticket).count(),
            "mine": db.query(Ticket)
            .filter(Ticket.assignee_id == current_user.id)
            .count(),
            "unassigned": db.query(Ticket)
            .filter(
                Ticket.assignee_id.is_(None),
                Ticket.status.in_(active_workflow_statuses),
            )
            .count(),
            "waiting": db.query(Ticket)
            .filter(Ticket.status == TicketStatus.AGUARDANDO_SOLICITANTE)
            .count(),
            "sla_risk": db.query(Ticket)
            .filter(
                Ticket.status.in_(active_workflow_statuses),
                Ticket.sla_paused_at.is_(None),
                risk_condition,
            )
            .count(),
            "overdue": db.query(Ticket)
            .filter(
                Ticket.status.in_(active_workflow_statuses),
                Ticket.sla_paused_at.is_(None),
                overdue_condition,
            )
            .count(),
        }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current_user": current_user,
            "tickets": tickets,
            "metrics": metrics,
            "active_queue": active_queue,
            "active_status": active_status.value if active_status else None,
            "active_priority": active_priority.value if active_priority else None,
            "active_category": active_category,
            "active_hub": active_hub,
            "category_options": [row[0] for row in db.query(Ticket.category).filter(Ticket.category.is_not(None)).distinct().order_by(Ticket.category).all()],
            "hub_options": [row[0] for row in db.query(Ticket.hub).filter(Ticket.hub.is_not(None)).distinct().order_by(Ticket.hub).all()],
            "search_query": search_query,
            "status_options": list(TicketStatus),
            "priority_options": list(TicketPriority),
            "page": page,
            "total_pages": total_pages,
            "total_items": total_items,
            "queue_counts": queue_counts,
            "sla_by_ticket": {
                ticket.id: sla_snapshot(ticket, now=now) for ticket in tickets
            },
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
    is_staff = current_user.role in {ROLE_ADMIN, ROLE_TECHNICIAN}
    if ticket and not (
        is_staff
        or (
            current_user.role == ROLE_REQUESTER
            and ticket.requester_id == current_user.id
        )
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
    projetos: list[dict] = []
    projetos_error = None
    if is_staff:
        projetos, projetos_error = _load_projetos()
    visible_comments = [
        comment
        for comment in ticket.comments
        if is_staff or not comment.is_internal
    ]
    return templates.TemplateResponse(
        request,
        "ticket_detail.html",
        {
            "current_user": current_user,
            "ticket": ticket,
            "technicians": technicians,
            "comments": visible_comments,
            "status_options": allowed_statuses(
                ticket, requester=current_user.role == ROLE_REQUESTER
            ),
            "priority_options": list(TicketPriority),
            "projetos": projetos,
            "projetos_error": projetos_error,
            "known_hubs": _known_hubs(db, ticket) if is_staff else [],
            "sla": sla_snapshot(ticket),
            "is_staff": is_staff,
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
    previous_role = user.role
    user.role = role
    user.is_technician = role in {ROLE_ADMIN, ROLE_TECHNICIAN}
    if previous_role != role:
        record_event(
            db,
            "user.role.changed",
            actor=current_user,
            summary="Perfil de acesso alterado.",
            changes={
                "user_id": user.id,
                "before": {"role": previous_role},
                "after": {"role": role},
            },
            source="web",
            category="security",
            resource_type="user",
            resource_id=str(user.id),
        )
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
    record_event(
        db,
        "ldap.sync.completed",
        actor=current_user,
        summary="Sincronizacao de usuarios do diretorio concluida.",
        changes={
            "count": len(ldap_users),
            "created_count": created,
            "updated_count": updated,
        },
        source="web",
        category="security",
        resource_type="directory",
        resource_id="ldap",
    )
    db.commit()
    return RedirectResponse(
        f"/admin/usuarios?sincronizados={len(ldap_users)}"
        f"&criados={created}&atualizados={updated}",
        status_code=303,
    )
