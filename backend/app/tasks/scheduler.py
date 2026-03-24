import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


def setup_scheduler() -> None:
    from app.services.universe_scanner import run_universe_scan
    from app.services.hot_monitor import run_hot_monitor
    from app.services.focus_analyzer import run_focus_analysis

    scheduler.add_job(
        run_universe_scan,
        trigger=IntervalTrigger(seconds=settings.universe_scan_interval),
        id="universe_scan",
        name="Universe layer scan",
        replace_existing=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        run_hot_monitor,
        trigger=IntervalTrigger(seconds=settings.hot_poll_interval),
        id="hot_monitor",
        name="Hot layer monitor",
        replace_existing=True,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        run_focus_analysis,
        trigger=IntervalTrigger(seconds=settings.focus_poll_interval),
        id="focus_analysis",
        name="Focus layer analysis",
        replace_existing=True,
        misfire_grace_time=15,
    )
    logger.info("Scheduler configured: universe=%ds hot=%ds focus=%ds",
                settings.universe_scan_interval, settings.hot_poll_interval, settings.focus_poll_interval)
