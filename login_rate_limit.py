"""Limitador de autenticação por IP e identidade."""
from __future__ import annotations

import hashlib
from datetime import timedelta

from config import settings
from database import LoginAttempt, SessionLocal, utcnow


def _keys(ip: str, identity: str) -> tuple[str, str]:
    return tuple(hashlib.sha256(value.encode()).hexdigest() for value in (f"ip:{ip}", f"identity:{identity.strip().lower()}"))


def retry_after(ip: str, identity: str) -> int:
    now = utcnow()
    cutoff = now - timedelta(seconds=settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS)
    db = SessionLocal()
    try:
        waits = []
        for key in _keys(ip, identity):
            rows = db.query(LoginAttempt).filter(LoginAttempt.key_hash == key, LoginAttempt.occurred_at >= cutoff).order_by(LoginAttempt.occurred_at).all()
            if len(rows) >= settings.LOGIN_RATE_LIMIT_ATTEMPTS:
                waits.append(settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS - int((now - rows[0].occurred_at).total_seconds()))
        return max(waits, default=0)
    finally:
        db.close()


def register_failure(ip: str, identity: str) -> None:
    db = SessionLocal()
    try:
        db.add_all([LoginAttempt(key_hash=key) for key in _keys(ip, identity)])
        db.commit()
    finally:
        db.close()


def clear_identity(identity: str) -> None:
    key = _keys("ignored", identity)[1]
    db = SessionLocal()
    try:
        db.query(LoginAttempt).filter(LoginAttempt.key_hash == key).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
