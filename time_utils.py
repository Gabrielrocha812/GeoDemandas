"""Conversão consistente dos datetimes UTC legados para API e interface."""
from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import settings


@lru_cache(maxsize=1)
def _application_timezone() -> tzinfo:
    try:
        return ZoneInfo(settings.APP_TIMEZONE)
    except ZoneInfoNotFoundError:
        # UTC é um fallback seguro; a aplicação continua iniciando e o erro de
        # configuração não altera silenciosamente os instantes armazenados.
        return UTC


def as_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware_utc = (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
    return aware_utc.astimezone(_application_timezone())


def format_local_datetime(
    value: datetime | None,
    pattern: str = "%d/%m/%Y %H:%M",
) -> str:
    local_value = as_local(value)
    return local_value.strftime(pattern) if local_value is not None else "—"


def iso_utc(value: datetime | None) -> str | None:
    """ISO 8601 explícito em UTC para o navegador não interpretar como hora local."""
    if value is None:
        return None
    aware_utc = (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
    return aware_utc.isoformat().replace("+00:00", "Z")
