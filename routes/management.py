"""Indicadores gerenciais e exportação das demandas."""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from io import BytesIO, StringIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from auth import ROLE_ADMIN, ROLE_TECHNICIAN, require_roles
from business_time import business_hours_between
from database import SatisfactionRating, Ticket, TicketPriority, TicketStatus, User, get_db
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


def _xlsx_bytes(headers: list[str], rows: list[list]) -> bytes:
    """Gera uma planilha XLSX simples sem dependência de runtime adicional."""
    all_rows = [headers, *rows]
    sheet_rows = []
    for row_index, row in enumerate(all_rows, 1):
        cells = []
        for column_index, value in enumerate(row, 1):
            column = ""
            current = column_index
            while current:
                current, remainder = divmod(current - 1, 26)
                column = chr(65 + remainder) + column
            cells.append(f'<c r="{column}{row_index}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetData>' + "".join(sheet_rows) + '</sheetData><autoFilter ref="A1:K' + str(len(all_rows)) + '"/></worksheet>'
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Demandas" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


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
    ticket_ids = [ticket.id for ticket in tickets]
    csat = db.query(func.avg(SatisfactionRating.score), func.count(SatisfactionRating.id)).filter(SatisfactionRating.ticket_id.in_(ticket_ids)).one() if ticket_ids else (None, 0)
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
            "csat": round(float(csat[0]), 1) if csat[0] is not None else None,
            "csat_count": int(csat[1]),
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
        return Response(_xlsx_bytes(headers, rows), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="demandas.xlsx"'})
    output = StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(headers)
    writer.writerows(rows)
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="demandas.csv"'})
