"""Monitoramento, alertas e relatorios gerenciais agendados."""
from __future__ import annotations

import asyncio
import csv
import html
from io import StringIO
import logging
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func

from config import settings
from database import NotificationOutbox, ReportSchedule, SessionLocal, SystemAlert, Ticket, TicketStatus, utcnow
from notification_service import send_custom_email
from operational_health import beat, worker_status
from teams_service import send_teams_notification
from routes.management import _xlsx_bytes

logger = logging.getLogger("geodemandas.automation")


def _recipients() -> list[str]:
    configured = settings.MONITOR_ALERT_EMAILS or settings.INITIAL_ADMIN_EMAILS
    return sorted({item.strip().lower() for item in configured.split(",") if item.strip()})


def _notify(title: str, message: str, severity: str) -> bool:
    delivered = send_teams_notification(title, message, severity=severity)
    body = f"<h2>{html.escape(title)}</h2><p>{html.escape(message)}</p><p><a href='{html.escape(settings.APP_BASE_URL)}'>Abrir GeoDemandas</a></p>"
    for recipient in _recipients():
        delivered = send_custom_email(recipient, f"[GeoDemandas] {title}", body, f"{title}\n\n{message}\n{settings.APP_BASE_URL}") or delivered
    return delivered


def monitoring_cycle() -> None:
    now = utcnow()
    conditions: dict[str, tuple[str, str, str]] = {}
    for worker, state in worker_status(max_age_seconds=settings.WORKER_HEARTBEAT_MAX_AGE_SECONDS, now=now).items():
        if not state["healthy"]:
            conditions[f"worker:{worker}"] = ("critical", f"Worker {worker} indisponivel", "O processamento automatico deixou de atualizar o heartbeat.")
    usage = shutil.disk_usage(Path(settings.UPLOAD_DIR).resolve().anchor or "/")
    free_percent = int((usage.free / usage.total) * 100) if usage.total else 0
    if free_percent < settings.MONITOR_DISK_MIN_FREE_PERCENT:
        conditions["disk:free"] = ("critical", "Espaco em disco baixo", f"Restam {free_percent}% livres no volume da aplicacao.")

    db = SessionLocal()
    try:
        failed = db.query(func.count(NotificationOutbox.id)).filter(NotificationOutbox.status == "failed").scalar() or 0
        if failed:
            conditions["outbox:failed"] = ("warning", "Falhas de notificacao", f"Existem {failed} notificacoes com falha definitiva.")
        open_alerts = {item.fingerprint: item for item in db.query(SystemAlert).filter(SystemAlert.status == "open").all()}
        for fingerprint, (severity, title, message) in conditions.items():
            alert = open_alerts.pop(fingerprint, None)
            if alert is None:
                alert = SystemAlert(fingerprint=fingerprint, severity=severity, title=title, message=message, status="open", opened_at=now)
                db.add(alert)
                db.flush()
                if _notify(title, message, severity):
                    alert.notified_at = now
        for alert in open_alerts.values():
            alert.status = "resolved"
            alert.resolved_at = now
            _notify(f"Resolvido: {alert.title}", "A verificacao voltou ao estado normal.", "resolved")
        db.commit()
    finally:
        db.close()
    beat("automation", now=now)


def _next_run(schedule: ReportSchedule, now):
    if schedule.frequency == "daily":
        return now + timedelta(days=1)
    if schedule.frequency == "monthly":
        return now + timedelta(days=30)
    return now + timedelta(days=7)


def _report_attachment(db, report_format: str) -> tuple[str, bytes, str]:
    rows = db.query(Ticket).order_by(Ticket.created_at.desc()).all()
    headers = ["ID", "Assunto", "Status", "Prioridade", "Categoria", "Projeto", "Origem", "Criada em"]
    values = [[ticket.id, ticket.subject, ticket.status.value, ticket.priority.value, ticket.category or "", ticket.project_name or "", ticket.source_channel, ticket.created_at.isoformat()] for ticket in rows]
    if report_format == "csv":
        stream = StringIO()
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(headers)
        writer.writerows(values)
        return "geodemandas.csv", ("\ufeff" + stream.getvalue()).encode("utf-8"), "text/csv"
    return "geodemandas.xlsx", _xlsx_bytes(headers, values), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def scheduled_reports_cycle() -> None:
    now = utcnow()
    db = SessionLocal()
    try:
        schedules = db.query(ReportSchedule).filter(ReportSchedule.is_active.is_(True), ReportSchedule.next_run_at <= now).all()
        for schedule in schedules:
            total = db.query(func.count(Ticket.id)).scalar() or 0
            open_count = db.query(func.count(Ticket.id)).filter(Ticket.status.notin_([TicketStatus.CONCLUIDO, TicketStatus.CANCELADO])).scalar() or 0
            completed = db.query(func.count(Ticket.id)).filter(Ticket.status == TicketStatus.CONCLUIDO).scalar() or 0
            report_url = f"{settings.APP_BASE_URL.rstrip('/')}/admin/demandas/exportar?format={schedule.report_format}"
            body = (
                f"<h2>{html.escape(schedule.name)}</h2><p>Resumo automatico do GeoDemandas.</p>"
                f"<ul><li>Total: {total}</li><li>Em atendimento: {open_count}</li><li>Concluidas: {completed}</li></ul>"
                f"<p><a href='{html.escape(report_url)}'>Baixar relatorio {html.escape(schedule.report_format.upper())}</a></p>"
            )
            attachment = _report_attachment(db, schedule.report_format)
            ok = send_custom_email(schedule.recipient, f"[GeoDemandas] {schedule.name}", body, f"Total: {total}; em atendimento: {open_count}; concluidas: {completed}.\n{report_url}", attachment=attachment)
            schedule.last_run_at = now
            schedule.last_status = "sent" if ok else "failed"
            schedule.last_error = None if ok else "Provedor de e-mail nao entregou o relatorio"
            schedule.next_run_at = _next_run(schedule, now)
        db.commit()
    finally:
        db.close()


async def automation_worker_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(monitoring_cycle)
            await asyncio.to_thread(scheduled_reports_cycle)
        except Exception:
            logger.exception("Falha no ciclo de automacao")
        await asyncio.sleep(max(30, min(settings.MONITOR_INTERVAL_SECONDS, settings.SCHEDULED_REPORT_POLL_INTERVAL_SECONDS)))
