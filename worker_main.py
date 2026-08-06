"""Processo dedicado aos workers IMAP, outbox e SLA."""
import asyncio

from database import init_db
from email_worker import email_worker_loop
from outbox_service import notification_outbox_worker_loop
from sla_monitor import sla_alert_worker_loop
from automation_worker import automation_worker_loop


async def main() -> None:
    init_db()
    await asyncio.gather(email_worker_loop(), notification_outbox_worker_loop(), sla_alert_worker_loop(), automation_worker_loop())


if __name__ == "__main__":
    asyncio.run(main())
