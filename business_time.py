"""Cálculos de SLA no calendário comercial configurado."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from config import settings


def _is_business_day(value: datetime, extra_holidays: set[str] | None = None) -> bool:
    holidays = {item.strip() for item in settings.SLA_HOLIDAYS.split(",") if item.strip()}
    holidays.update(extra_holidays or set())
    return value.weekday() < 5 and value.date().isoformat() not in holidays


def _local(value: datetime) -> datetime:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.astimezone(ZoneInfo(settings.APP_TIMEZONE))


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _bounds(value: datetime) -> tuple[datetime, datetime]:
    start = value.replace(hour=settings.SLA_BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)
    end = value.replace(hour=settings.SLA_BUSINESS_END_HOUR, minute=0, second=0, microsecond=0)
    return start, end


def _next_open(value: datetime, extra_holidays: set[str] | None = None) -> datetime:
    current = value
    while True:
        start, end = _bounds(current)
        if _is_business_day(current, extra_holidays) and current < end:
            return max(current, start)
        current = (current + timedelta(days=1)).replace(hour=settings.SLA_BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)


def add_business_hours(value: datetime, hours: float, *, holidays: set[str] | None = None) -> datetime:
    """Soma horas úteis e devolve UTC ingênuo, como as colunas legadas."""
    current = _next_open(_local(value), holidays)
    remaining = timedelta(hours=max(0, hours))
    while remaining > timedelta(0):
        _, end = _bounds(current)
        available = end - current
        if remaining <= available:
            return _utc_naive(current + remaining)
        remaining -= available
        current = _next_open((current + timedelta(days=1)).replace(hour=settings.SLA_BUSINESS_START_HOUR, minute=0, second=0, microsecond=0), holidays)
    return _utc_naive(current)


def business_hours_between(start: datetime, end: datetime, *, holidays: set[str] | None = None) -> float:
    """Conta somente a interseção com dias úteis e a jornada configurada."""
    local_start, local_end = _local(start), _local(end)
    if local_end <= local_start:
        return 0.0
    total = timedelta(0)
    day = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day.date() <= local_end.date():
        if _is_business_day(day, holidays):
            open_at, close_at = _bounds(day)
            interval_start = max(local_start, open_at)
            interval_end = min(local_end, close_at)
            if interval_end > interval_start:
                total += interval_end - interval_start
        day += timedelta(days=1)
    return total.total_seconds() / 3600
