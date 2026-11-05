"""APScheduler wiring: the mail poller and the daily summary."""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings, get_settings
from app.services import check_new_mail, daily_summary

logger = logging.getLogger(__name__)

JOB_POLL = "poll_new_mail"
JOB_SUMMARY = "daily_summary"

_scheduler: AsyncIOScheduler | None = None


def build_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    """Create the scheduler with both jobs registered."""

    settings = settings or get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.summary_tz)

    scheduler.add_job(
        check_new_mail,
        trigger="interval",
        minutes=settings.poll_interval_minutes,
        id=JOB_POLL,
        name="Poll Gmail for new mail",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        daily_summary,
        trigger="cron",
        hour=settings.summary_hour,
        minute=settings.summary_minute,
        id=JOB_SUMMARY,
        name="Daily email summary",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler


def start_scheduler() -> AsyncIOScheduler:
    """Start the scheduler (idempotent)."""

    global _scheduler
    if _scheduler is None:
        _scheduler = build_scheduler()
    if not _scheduler.running:
        _scheduler.start()
        logger.info(
            "Scheduler started",
            extra={
                "poll_interval_minutes": get_settings().poll_interval_minutes,
                "summary_hour": get_settings().summary_hour,
                "summary_timezone": get_settings().summary_timezone,
            },
        )
    return _scheduler


def stop_scheduler() -> None:
    """Stop the scheduler if it is running."""

    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
    _scheduler = None


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the running scheduler, if any."""

    return _scheduler


def scheduler_status() -> dict[str, Any]:
    """Return a serialisable description of the scheduler state."""

    scheduler = _scheduler
    if scheduler is None:
        return {"running": False, "jobs": []}
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            }
        )
    return {"running": bool(scheduler.running), "jobs": jobs}
