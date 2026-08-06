"""Busca avancada, conhecimento e configuracoes operacionais."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import String, cast, or_
from sqlalchemy.orm import Session, joinedload

from auth import ROLE_ADMIN, ROLE_TECHNICIAN, User, get_current_user, require_roles
from database import (
    AuditEvent,
    BusinessHoliday,
    Comment,
    KnowledgeArticle,
    ReportSchedule,
    SlaPolicy,
    SystemAlert,
    Ticket,
    TicketPriority,
    TicketStatus,
    get_db,
    utcnow,
)
from routes.web import templates
from workflow_service import sla_snapshot

router = APIRouter()


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:170] or "artigo"


@router.get("/conhecimento", response_class=HTMLResponse)
def knowledge_base(request: Request, q: str | None = None, category: str | None = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    search = (q or "").strip()[:160]
    active_category = (category or "").strip()[:120]
    query = db.query(KnowledgeArticle).filter(KnowledgeArticle.is_published.is_(True))
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(KnowledgeArticle.title.ilike(pattern), KnowledgeArticle.summary.ilike(pattern), KnowledgeArticle.content.ilike(pattern), KnowledgeArticle.tags.ilike(pattern)))
    if active_category:
        query = query.filter(KnowledgeArticle.category == active_category)
    articles = query.order_by(KnowledgeArticle.view_count.desc(), KnowledgeArticle.updated_at.desc()).limit(100).all()
    categories = [row[0] for row in db.query(KnowledgeArticle.category).filter(KnowledgeArticle.is_published.is_(True)).distinct().order_by(KnowledgeArticle.category).all()]
    return templates.TemplateResponse(request, "knowledge_base.html", {"current_user": current_user, "articles": articles, "categories": categories, "search_query": search, "active_category": active_category})


@router.get("/conhecimento/{slug}", response_class=HTMLResponse)
def knowledge_article(slug: str, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    article = db.query(KnowledgeArticle).filter(KnowledgeArticle.slug == slug, KnowledgeArticle.is_published.is_(True)).first()
    if article is None:
        raise HTTPException(status_code=404, detail="Artigo nao encontrado")
    article.view_count += 1
    db.commit()
    related = db.query(KnowledgeArticle).filter(KnowledgeArticle.is_published.is_(True), KnowledgeArticle.category == article.category, KnowledgeArticle.id != article.id).limit(4).all()
    return templates.TemplateResponse(request, "knowledge_article.html", {"current_user": current_user, "article": article, "related": related})


@router.get("/admin/conhecimento", response_class=HTMLResponse)
def admin_knowledge(request: Request, current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)), db: Session = Depends(get_db)):
    articles = db.query(KnowledgeArticle).order_by(KnowledgeArticle.updated_at.desc()).all()
    return templates.TemplateResponse(request, "admin_knowledge.html", {"current_user": current_user, "articles": articles})


@router.post("/admin/conhecimento")
def create_article(title: str = Form(...), summary: str = Form(...), content: str = Form(...), category: str = Form(...), tags: str = Form(""), published: bool = Form(False), current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)), db: Session = Depends(get_db)):
    title = title.strip()[:220]
    if len(title) < 4 or len(summary.strip()) < 10 or len(content.strip()) < 20:
        raise HTTPException(status_code=422, detail="Titulo, resumo ou conteudo insuficiente")
    base = _slug(title)
    slug = base
    suffix = 2
    while db.query(KnowledgeArticle.id).filter(KnowledgeArticle.slug == slug).first():
        slug, suffix = f"{base[:160]}-{suffix}", suffix + 1
    db.add(KnowledgeArticle(slug=slug, title=title, summary=summary.strip()[:500], content=content.strip(), category=category.strip()[:120], tags=tags.strip()[:500] or None, is_published=published, created_by_id=current_user.id))
    db.commit()
    return RedirectResponse("/admin/conhecimento?criado=1", status_code=303)


@router.post("/admin/conhecimento/{article_id}/publicacao")
def toggle_article(article_id: int, current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)), db: Session = Depends(get_db)):
    article = db.get(KnowledgeArticle, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Artigo nao encontrado")
    article.is_published = not article.is_published
    article.updated_at = utcnow()
    db.commit()
    return RedirectResponse("/admin/conhecimento", status_code=303)


@router.get("/admin/busca", response_class=HTMLResponse)
def advanced_search(request: Request, q: str | None = None, status: str | None = None, priority: str | None = None, source: str | None = None, assignee: str | None = None, created_from: date | None = None, created_to: date | None = None, sla: str | None = None, current_user: User = Depends(require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)), db: Session = Depends(get_db)):
    search = (q or "").strip()[:200]
    query = db.query(Ticket).options(joinedload(Ticket.requester), joinedload(Ticket.assignee))
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(cast(Ticket.id, String).ilike(pattern), Ticket.subject.ilike(pattern), Ticket.body.ilike(pattern), Ticket.project_name.ilike(pattern), Ticket.comments.any(Comment.content.ilike(pattern))))
    if status in {item.value for item in TicketStatus}:
        query = query.filter(Ticket.status == TicketStatus(status))
    if priority in {item.value for item in TicketPriority}:
        query = query.filter(Ticket.priority == TicketPriority(priority))
    if source in {"portal", "email"}:
        query = query.filter(Ticket.source_channel == source)
    if assignee == "unassigned":
        query = query.filter(Ticket.assignee_id.is_(None))
    elif assignee and assignee.isdigit():
        query = query.filter(Ticket.assignee_id == int(assignee))
    if created_from:
        query = query.filter(Ticket.created_at >= datetime.combine(created_from, datetime.min.time()))
    if created_to:
        query = query.filter(Ticket.created_at < datetime.combine(created_to + timedelta(days=1), datetime.min.time()))
    candidates = query.order_by(Ticket.last_activity_at.desc()).limit(500).all()
    if sla in {"ok", "risk", "overdue", "paused", "done"}:
        candidates = [ticket for ticket in candidates if sla_snapshot(ticket)["state"] == sla]
    ticket_ids = [ticket.id for ticket in candidates]
    history = db.query(AuditEvent).filter(AuditEvent.ticket_id.in_(ticket_ids)).order_by(AuditEvent.created_at.desc()).limit(100).all() if ticket_ids else []
    technicians = db.query(User).filter(User.role.in_([ROLE_ADMIN, ROLE_TECHNICIAN]), User.is_active.is_(True)).order_by(User.full_name).all()
    filters = {"q": search, "status": status or "", "priority": priority or "", "source": source or "", "assignee": assignee or "", "created_from": created_from, "created_to": created_to, "sla": sla or ""}
    return templates.TemplateResponse(request, "advanced_search.html", {"current_user": current_user, "tickets": candidates[:100], "history": history, "technicians": technicians, "status_options": list(TicketStatus), "priority_options": list(TicketPriority), "sla_by_ticket": {ticket.id: sla_snapshot(ticket) for ticket in candidates[:100]}, "filters": filters})


@router.get("/admin/configuracoes", response_class=HTMLResponse)
def admin_settings(request: Request, current_user: User = Depends(require_roles(ROLE_ADMIN)), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "admin_settings.html", {"current_user": current_user, "policies": db.query(SlaPolicy).order_by(SlaPolicy.is_active.desc(), SlaPolicy.name).all(), "holidays": db.query(BusinessHoliday).order_by(BusinessHoliday.holiday_date).all(), "schedules": db.query(ReportSchedule).order_by(ReportSchedule.created_at.desc()).all(), "alerts": db.query(SystemAlert).order_by(SystemAlert.opened_at.desc()).limit(30).all(), "priority_options": list(TicketPriority)})


@router.post("/admin/configuracoes/sla")
def create_sla_policy(name: str = Form(...), priority: str = Form(...), first_response_hours: int = Form(...), resolution_hours: int = Form(...), category: str = Form(""), project_code: str = Form(""), current_user: User = Depends(require_roles(ROLE_ADMIN)), db: Session = Depends(get_db)):
    if priority not in {item.value for item in TicketPriority} or not 1 <= first_response_hours <= 720 or not 1 <= resolution_hours <= 2160:
        raise HTTPException(status_code=422, detail="Politica de SLA invalida")
    db.add(SlaPolicy(name=name.strip()[:160], priority=priority, category=category.strip()[:120] or None, project_code=int(project_code) if project_code.strip().isdigit() else None, first_response_hours=first_response_hours, resolution_hours=resolution_hours))
    db.commit()
    return RedirectResponse("/admin/configuracoes?sla=1", status_code=303)


@router.post("/admin/configuracoes/feriados")
def create_holiday(holiday_date: date = Form(...), name: str = Form(...), current_user: User = Depends(require_roles(ROLE_ADMIN)), db: Session = Depends(get_db)):
    if db.query(BusinessHoliday.id).filter(BusinessHoliday.holiday_date == holiday_date).first() is None:
        db.add(BusinessHoliday(holiday_date=holiday_date, name=name.strip()[:160]))
        db.commit()
    return RedirectResponse("/admin/configuracoes?feriado=1", status_code=303)


@router.post("/admin/configuracoes/relatorios")
def create_report_schedule(name: str = Form(...), recipient: str = Form(...), frequency: str = Form(...), report_format: str = Form("xlsx"), current_user: User = Depends(require_roles(ROLE_ADMIN)), db: Session = Depends(get_db)):
    if frequency not in {"daily", "weekly", "monthly"} or report_format not in {"csv", "xlsx"} or "@" not in recipient:
        raise HTTPException(status_code=422, detail="Agendamento invalido")
    db.add(ReportSchedule(name=name.strip()[:160], recipient=recipient.strip().lower()[:255], frequency=frequency, report_format=report_format, filters_json=json.dumps({}), next_run_at=utcnow() + timedelta(minutes=2), created_by_id=current_user.id))
    db.commit()
    return RedirectResponse("/admin/configuracoes?relatorio=1", status_code=303)


@router.post("/admin/configuracoes/{kind}/{item_id}/alternar")
def toggle_setting(kind: str, item_id: int, current_user: User = Depends(require_roles(ROLE_ADMIN)), db: Session = Depends(get_db)):
    model = {"sla": SlaPolicy, "relatorio": ReportSchedule}.get(kind)
    item = db.get(model, item_id) if model else None
    if item is None:
        raise HTTPException(status_code=404, detail="Configuracao nao encontrada")
    item.is_active = not item.is_active
    db.commit()
    return RedirectResponse("/admin/configuracoes", status_code=303)
