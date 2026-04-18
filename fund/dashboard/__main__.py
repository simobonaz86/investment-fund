"""Launch dashboard + background scheduler in one process."""
from __future__ import annotations

import logging

import uvicorn

from fund.config import settings
from fund.dashboard.app import app
from fund.scheduler import build_scheduler

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dashboard")


def main() -> None:
    sched = build_scheduler()
    sched.start()
    log.info("Scheduler started with %d jobs.", len(sched.get_jobs()))
    try:
        uvicorn.run(
            app,
            host=settings.DASHBOARD_HOST,
            port=settings.DASHBOARD_PORT,
            log_level=settings.LOG_LEVEL.lower(),
        )
    finally:
        sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
