"""APScheduler wiring for Phase 2.2.

Scheduled jobs (all UTC):
  * HR weekly review     — Monday 09:00
  * Daily report         — every day 18:00
  * Weekly report        — Monday 09:30
  * Monthly report       — 1st of month 09:00
  * Quarterly report     — 1st of Jan/Apr/Jul/Oct 09:30
  * YTD report           — 1st of month 10:00
  * Board inbox poll     — every 30 seconds
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from fund.agents.ceo import process_board_inbox
from fund.agents.hr import run_hr_review
from fund.config import settings
from fund.reports.generator import generate_report

log = logging.getLogger(__name__)


def _safe(fn, *args, **kwargs):
    def wrapper():
        try:
            result = fn(*args, **kwargs)
            log.info("%s completed: %s", fn.__name__,
                     {k: v for k, v in result.items() if k != "markdown"}
                     if isinstance(result, dict) else result)
        except Exception:
            log.exception("%s failed", fn.__name__)
    return wrapper


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")

    sched.add_job(
        _safe(run_hr_review),
        CronTrigger(day_of_week=settings.HR_REVIEW_DAY.lower(),
                    hour=settings.HR_REVIEW_HOUR_UTC, minute=0),
        id="hr_weekly",
    )

    sched.add_job(_safe(generate_report, "daily"),
                  CronTrigger(hour=settings.DAILY_REPORT_HOUR, minute=0),
                  id="report_daily")
    sched.add_job(_safe(generate_report, "weekly"),
                  CronTrigger(day_of_week=settings.WEEKLY_REPORT_DAY.lower(),
                              hour=settings.WEEKLY_REPORT_HOUR, minute=30),
                  id="report_weekly")
    sched.add_job(_safe(generate_report, "monthly"),
                  CronTrigger(day=settings.MONTHLY_REPORT_DAY,
                              hour=settings.MONTHLY_REPORT_HOUR, minute=0),
                  id="report_monthly")
    sched.add_job(_safe(generate_report, "quarterly"),
                  CronTrigger(month="1,4,7,10", day=1, hour=9, minute=30),
                  id="report_quarterly")
    sched.add_job(_safe(generate_report, "ytd"),
                  CronTrigger(day=1, hour=10, minute=0),
                  id="report_ytd")

    # Board → CEO inbox poll. Runs every 30 s; no-op when nothing to read.
    sched.add_job(_safe(process_board_inbox),
                  IntervalTrigger(seconds=30),
                  id="board_inbox_poll", max_instances=1, coalesce=True)

    return sched
