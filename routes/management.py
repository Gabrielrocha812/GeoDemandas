"""Indicadores gerenciais e exportação das demandas."""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from io import BytesIO, StringIO

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from openpyxl import Workbook
from sqlalchemy.orm import Session, joinedload

from auth import ROLE_ADMIN, ROLE_TECHNICIAN, require_roles
from business_time import business_hours_between
from database import Ticket, TicketPriority, TicketStatus, User, get_db
from routes.web import templates
from time_utils import format_local_datetime

router = APIRouter()
_staff = require_roles(ROLE_ADMIN, ROLE_TECHNICIAN)


def _filtered_tickets(
    db: Session,
    *,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    hub: str | None = None,
):
    query = db.query(Ticket).options(joinedload(Ticket.requester), joinedload(Ticket.assignee))
    if status in {item.value for item in TicketStatus}:
        query = query.filter(Ticket.status == TicketStatus(status))
    if priority in {item.value for item in TicketPriority}:
        query = query.filter(Ticket.priority == TicketPriority(priority))
    if category:
        query = query.filter(Ticket.category == category.strip()[:120])
    if hub:
        query = query.filter(Ticket.hub == hub.strip()[:255])
    return query


def _hours(end: datetime | None, start: datetime | None) -> float | None:
    if not end or not start:
        return None
    return business_hours_between(start, end)


@router.get("/admin/indicadores", response_class=HTMLResponse)
def management_dashboard(
    request: Request,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    hub: str | None = None,
    current_user: User = Depends(_staff),
    db: Session = Depends(get_db),
):
    tickets = _filtered_tickets(
        db, status=status, priority=priority, category=category, hub=hub
    ).order_by(Ticket.created_at.desc()).all()
    first_samples = [_hours(t.first_response_at, t.created_at) for t in tickets]
    resolution_samples = [_hours(t.resolved_at, t.created_at) for t in tickets]
    first_samples = [value for value in first_samples if value is not None]
    resolution_samples = [value for value in resolution_samples if value is not None]
    first_sla = [t.first_response_at <= t.first_response_due_at for t in tickets if t.first_response_at and t.first_response_due_at]
    resolution_sla = [t.resolved_at <= t.resolution_due_at for t in tickets if t.resolved_at and t.resolution_due_at]
    by_month = Counter(t.created_at.strftime("%Y-%m") for t in tickets)
    by_requester = Counter(t.requester.full_name for t in tickets)
    return templates.TemplateResponse(request, "management_dashboard.html", {
        "current_user": current_user,
        "tickets": tickets,
        "filters": {"status": status or "", "priority": priority or "", "category": category or "", "hub": hub or ""},
        "status_options": list(TicketStatus),
        "priority_options": list(TicketPriority),
        "categories": sorted({t.category for t in db.query(Ticket).all() if t.category}, key=str.casefold),
        "hubs": sorted({t.hub for t in db.query(Ticket).all() if t.hub}, key=str.casefold),
        "metrics": {
            "total": len(tickets),
            "first_sla": round(100 * sum(first_sla) / len(first_sla), 1) if first_sla else None,
            "resolution_sla": round(100 * sum(resolution_sla) / len(resolution_sla), 1) if resolution_sla else None,
            "first_hours": round(sum(first_samples) / len(first_samples), 1) if first_samples else None,
            "resolution_hours": round(sum(resolution_samples) / len(resolution_samples), 1) if resolution_samples else None,
        },
        "by_month": sorted(by_month.items(), reverse=True)[:12],
        "by_category": Counter(t.category or "Sem categoria" for t in tickets).most_common(8),
        "by_hub": Counter(t.hub or "Sem hub" for t in tickets).most_common(8),
        "by_requester": by_requester.most_common(8),
    })


@router.get("/admin/demandas/exportar")
def export_tickets(
    format: str = "csv",
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    hub: str | None = None,
    current_user: User = Depends(_staff),
    db: Session = Depends(get_db),
):
    tickets = _filtered_tickets(db, status=status, priority=priority, category=category, hub=hub).order_by(Ticket.created_at.desc()).all()
    headers = ["ID", "Assunto", "Status", "Prioridade", "Categoria", "Hub", "Solicitante", "Responsável", "Criada em", "Primeira resposta", "Resolvida em"]
    rows = [[t.id, t.subject, t.status.value, t.priority.value, t.category or "", t.hub or "", t.requester.full_name, t.assignee.full_name if t.assignee else "", format_local_datetime(t.created_at), format_local_datetime(t.first_response_at), format_local_datetime(t.resolved_at)] for t in tickets]
    if format.lower() == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Demandas"
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        output = BytesIO()
        workbook.save(output)
        return Response(output.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="demandas.xlsx"'})
    output = StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(headers)
    writer.writerows(rows)
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="demandas.csv"'})
