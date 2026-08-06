"""Heartbeats dos workers internos."""
from __future__ import annotations

from datetime import datetime
from database import SessionLocal, WorkerHeartbeat, utcnow


def beat(worker: str, *, now: datetime | None = None) -> None:
    db = SessionLocal()
    try:
        item = db.get(WorkerHeartbeat, worker)
        if item is None:
            db.add(WorkerHeartbeat(worker=worker, last_seen_at=now or utcnow()))
        else:
            item.last_seen_at = now or utcnow()
        db.commit()
    finally:
        db.close()


def worker_status(*, max_age_seconds: int, now: datetime | None = None) -> dict[str, dict]:
    current = now or utcnow()
    db = SessionLocal()
    try:
        snapshot = {item.worker: item.last_seen_at for item in db.query(WorkerHeartbeat).all()}
    finally:
        db.close()
    result = {}
    for worker in ("email", "outbox", "sla", "automation"):
        last_seen = snapshot.get(worker)
        age = (current - last_seen).total_seconds() if last_seen else None
        result[worker] = {"healthy": age is not None and age <= max_age_seconds, "last_seen": last_seen.isoformat() + "Z" if last_seen else None, "age_seconds": round(age, 1) if age is not None else None}
    return result
