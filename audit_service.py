"""Registro estruturado e seguro de eventos de auditoria.

Este módulo deliberadamente não registra conteúdo livre de demandas,
comentários, notificações ou requisições. O chamador fornece apenas metadados
estruturados; o evento entra na mesma transação da alteração de negócio e
somente será persistido se essa transação for confirmada.
"""
from __future__ import annotations

import enum
import ipaddress
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from database import AuditEvent


MAX_SUMMARY_LENGTH = 255
MAX_STRING_VALUE_LENGTH = 255
MAX_CHANGES_LENGTH = 8_000
MAX_COLLECTION_ITEMS = 64
MAX_NESTING_DEPTH = 6


class AuditPayloadRejected(ValueError):
    """O evento continha um campo ou valor que não pode ir para a auditoria."""


# Nomes que normalmente carregam conteúdo livre, credenciais, PII ou dados
# técnicos de uma requisição. Contagens e indicadores booleanos são avaliados
# antes desta lista, pois não carregam o conteúdo em si.
_FORBIDDEN_KEY_PARTS = {
    "body",
    "content",
    "text",
    "email",
    "mail",
    "password",
    "passwd",
    "token",
    "secret",
    "note",
    "description",
    "subject",
    "recipient",
    "raw",
    "response",
    "ip",
    "useragent",
    "authorization",
    "cookie",
    "session",
}

_ALLOWED_FIELDS = {
    "id",
    "status",
    "priority",
    "hub",
    "category",
    "project_code",
    "role",
    "is_active",
    "active",
    "has_note",
    "attachment_count",
    "count",
    "score",
    "attempts",
    "max_attempts",
    "event_type",
    "source_channel",
    "due_at",
    "created_at",
    "updated_at",
    "first_response_at",
    "first_response_due_at",
    "resolution_due_at",
    "resolved_at",
    "closed_at",
    "next_attempt_at",
}
_WRAPPER_FIELDS = {"before", "after"}

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,63}(?![\w.-])"
)
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+|"
    r"(?:password|passwd|token|secret|authorization)\s*[:=]"
)
_USER_AGENT_RE = re.compile(
    r"(?i)\b(?:mozilla/\d|curl/\d|postmanruntime/|python-requests/|"
    r"okhttp/|wget/)\b"
)
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_.:-]+$")


def _attribute(subject: Any, name: str, default: Any = None) -> Any:
    if subject is None:
        return default
    if isinstance(subject, Mapping):
        return subject.get(name, default)
    return getattr(subject, name, default)


def _key_parts(key: str) -> set[str]:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    return {part for part in re.split(r"[^a-z0-9]+", snake) if part}


def _is_allowed_field(key: str) -> bool:
    normalized = key.strip().lower()
    if not normalized:
        return False
    if normalized in _WRAPPER_FIELDS or normalized in _ALLOWED_FIELDS:
        return True
    # Identificadores e contagens são seguros por definição, desde que o valor
    # também passe pela validação abaixo.
    if normalized.endswith("_id") or normalized.endswith("_count"):
        return True
    if normalized.endswith("_status") or normalized.endswith("_at"):
        return True
    return False


def _reject_forbidden_key(key: str) -> None:
    normalized = key.strip().lower()
    if normalized.endswith("_count") or normalized == "has_note":
        return
    parts = _key_parts(key)
    if parts & _FORBIDDEN_KEY_PARTS:
        raise AuditPayloadRejected(
            f"O campo de auditoria '{key}' pode conter conteúdo sensível."
        )


def _looks_like_ip(value: str) -> bool:
    candidate = value.strip().strip("[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def _safe_string(value: str, *, field: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) > MAX_STRING_VALUE_LENGTH:
        raise AuditPayloadRejected(
            f"O valor de '{field}' excede o limite seguro de auditoria."
        )
    if _EMAIL_RE.search(normalized):
        raise AuditPayloadRejected(
            f"O valor de '{field}' contém um endereço de e-mail."
        )
    if _CREDENTIAL_RE.search(normalized):
        raise AuditPayloadRejected(
            f"O valor de '{field}' pode conter uma credencial."
        )
    if _USER_AGENT_RE.search(normalized):
        raise AuditPayloadRejected(
            f"O valor de '{field}' pode conter um user-agent."
        )
    if _looks_like_ip(normalized):
        raise AuditPayloadRejected(
            f"O valor de '{field}' contém um endereço IP."
        )
    return normalized


def _json_value(
    value: Any,
    *,
    field: str,
    parent_field: str | None,
    depth: int,
) -> Any:
    if depth > MAX_NESTING_DEPTH:
        raise AuditPayloadRejected("A estrutura de auditoria é profunda demais.")

    if isinstance(value, enum.Enum):
        return _json_value(
            value.value,
            field=field,
            parent_field=parent_field,
            depth=depth,
        )
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditPayloadRejected("A auditoria não aceita números não finitos.")
        return value
    if isinstance(value, str):
        # "before"/"after" escalares só são válidos dentro de um campo
        # semântico conhecido, como status ou priority.
        if field in _WRAPPER_FIELDS and parent_field is None:
            raise AuditPayloadRejected(
                f"'{field}' deve agrupar campos estruturados."
            )
        return _safe_string(value, field=parent_field or field)

    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise AuditPayloadRejected("Há campos demais no evento de auditoria.")
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise AuditPayloadRejected(
                    "Todas as chaves de auditoria devem ser texto."
                )
            key = raw_key.strip()
            _reject_forbidden_key(key)
            if not _is_allowed_field(key):
                raise AuditPayloadRejected(
                    f"O campo de auditoria '{key}' não está na lista segura."
                )
            next_parent = parent_field if key in _WRAPPER_FIELDS else key
            result[key] = _json_value(
                item,
                field=key,
                parent_field=next_parent,
                depth=depth + 1,
            )
        return result

    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise AuditPayloadRejected("A lista de auditoria é grande demais.")
        return [
            _json_value(
                item,
                field=field,
                parent_field=parent_field,
                depth=depth + 1,
            )
            for item in value
        ]

    raise AuditPayloadRejected(
        f"O tipo de valor de '{field}' não pode ser serializado com segurança."
    )


def serialize_changes(changes: Mapping[str, Any] | None) -> str:
    """Valida e serializa mudanças em JSON compacto e determinístico."""
    if changes is None:
        return "{}"
    if not isinstance(changes, Mapping):
        raise AuditPayloadRejected("'changes' deve ser um objeto estruturado.")

    safe = _json_value(
        changes,
        field="changes",
        parent_field=None,
        depth=0,
    )
    encoded = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > MAX_CHANGES_LENGTH:
        raise AuditPayloadRejected("O evento de auditoria excede o limite seguro.")
    return encoded


def _safe_identifier(value: Any, *, field: str, max_length: int = 100) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise AuditPayloadRejected(f"'{field}' é obrigatório.")
    if len(normalized) > max_length or not _IDENTIFIER_RE.fullmatch(normalized):
        raise AuditPayloadRejected(f"'{field}' não é um identificador válido.")
    return normalized


def _safe_summary(summary: Any) -> str:
    normalized = " ".join(str(summary or "").split())
    if not normalized:
        raise AuditPayloadRejected("'summary' é obrigatório.")
    if len(normalized) > MAX_SUMMARY_LENGTH:
        normalized = normalized[: MAX_SUMMARY_LENGTH - 1].rstrip() + "…"
    return _safe_string(normalized, field="summary")


def _actor_snapshot(actor: Any) -> tuple[int | None, str, str | None]:
    actor_id = _attribute(actor, "id")
    raw_name = _attribute(actor, "full_name") or _attribute(actor, "name")
    if raw_name:
        try:
            actor_name = _safe_string(str(raw_name), field="actor_name")
        except AuditPayloadRejected:
            actor_name = (
                f"Usuário #{actor_id}" if actor_id is not None else "Usuário autenticado"
            )
    else:
        actor_name = (
            f"Usuário #{actor_id}" if actor_id is not None else "Sistema"
        )

    raw_role = _attribute(actor, "role")
    actor_role = None
    if raw_role is not None:
        actor_role = _safe_identifier(
            raw_role.value if isinstance(raw_role, enum.Enum) else raw_role,
            field="actor_role",
            max_length=50,
        )
    return actor_id, actor_name[:255], actor_role


def record_event(
    db: Session,
    action: str,
    *,
    actor: Any = None,
    ticket: Any = None,
    ticket_id: int | None = None,
    summary: str,
    changes: Mapping[str, Any] | None = None,
    source: str = "web",
    category: str = "business",
    actor_type: str | None = None,
    resource_type: str = "ticket",
    resource_id: Any = None,
    correlation_id: str | None = None,
) -> AuditEvent:
    """Adiciona um evento à transação atual sem executar ``flush`` ou ``commit``."""
    object_ticket_id = _attribute(ticket, "id")
    if (
        ticket_id is not None
        and object_ticket_id is not None
        and ticket_id != object_ticket_id
    ):
        raise AuditPayloadRejected(
            "O ticket informado diverge do identificador explícito."
        )
    resolved_ticket_id = (
        ticket_id if ticket_id is not None else object_ticket_id
    )

    actor_id, actor_name, actor_role = _actor_snapshot(actor)
    resolved_actor_type = actor_type or ("user" if actor is not None else "system")
    resolved_resource_id = (
        resource_id
        if resource_id is not None
        else resolved_ticket_id
    )

    event = AuditEvent(
        event_uuid=str(uuid4()),
        ticket_id=resolved_ticket_id,
        actor_id=actor_id,
        actor_name=actor_name,
        actor_role=actor_role,
        actor_type=_safe_identifier(
            resolved_actor_type,
            field="actor_type",
            max_length=30,
        ),
        source=_safe_identifier(source, field="source", max_length=30),
        category=_safe_identifier(category, field="category", max_length=50),
        action=_safe_identifier(action, field="action", max_length=100),
        resource_type=_safe_identifier(
            resource_type,
            field="resource_type",
            max_length=50,
        ),
        resource_id=(
            _safe_identifier(
                resolved_resource_id,
                field="resource_id",
                max_length=100,
            )
            if resolved_resource_id is not None
            else None
        ),
        summary=_safe_summary(summary),
        changes_json=serialize_changes(changes),
        correlation_id=(
            _safe_identifier(
                correlation_id,
                field="correlation_id",
                max_length=100,
            )
            if correlation_id
            else None
        ),
    )
    db.add(event)
    return event


def changes_for_display(event: AuditEvent) -> dict[str, Any]:
    """Converte o JSON já sanitizado para uso no painel administrativo."""
    try:
        value = json.loads(event.changes_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
