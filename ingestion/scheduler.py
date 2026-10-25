from __future__ import annotations

import functools
import threading
from collections.abc import Callable

from apscheduler.schedulers.blocking import BlockingScheduler

from common.config import get_settings
from common.logging import get_logger
from forecast.engine import maybe_run_forecast
from ingestion.source_clients import ensure_background_probe
from ingestion.worker import run_once
from ops.parity import run_parity
from ops.retention import run_retention

log = get_logger(__name__)

# M7: cross-job mutual exclusion. APScheduler runs each job in its own thread
# with no cross-job coordination, and the retention job's VACUUM/compaction
# takes an exclusive SQLite lock — a scan overlapping it fails with
# "database is locked". Guard every job with a shared non-blocking lock: when
# another job is already running, the run is skipped (every job is periodic
# and retries on its next interval) instead of colliding on the database.
_JOB_LOCK = threading.Lock()


def _exclusive(job_name: str, fn: Callable[[], object]) -> Callable[[], object]:
    """Wrap a scheduler job so an overlapping run skips instead of colliding."""

    @functools.wraps(fn)
    def wrapper() -> object:
        if not _JOB_LOCK.acquire(blocking=False):
            log.warning("scheduler_job_skipped_overlap", job=job_name)
            return None
        try:
            return fn()
        finally:
            _JOB_LOCK.release()

    return wrapper


def _retention_job() -> None:
    """Scheduled retention pass: compact + prune + lake-growth report."""
    from storage.database import SessionLocal

    with SessionLocal() as session:
        result = run_retention(session)
        session.commit()
    log.info("retention_autopilot_run", result=result)


def _parity_job() -> None:
    """Daily lake-vs-SQL parity comparison over the archived lake."""
    from storage.database import SessionLocal

    with SessionLocal() as session:
        result = run_parity(session)
        session.commit()
    log.info("parity_check_run", result=result)


def _forecast_job() -> None:
    """Cadence-gated forecast retraining and drift-baseline persistence."""
    result = maybe_run_forecast()
    if result.get("status") != "skipped":
        log.info("forecast_training_run", result=result)


def _backtest_autopilot_job() -> None:
    """Scheduled walk-forward backtest with drift detection and alerting."""
    from ops.backtest_drift import run_backtest_autopilot
    from storage.database import SessionLocal

    with SessionLocal() as session:
        result = run_backtest_autopilot(session)
        session.commit()
    log.info("backtest_autopilot_run", result=result)


def main() -> None:
    settings = get_settings()
    ensure_background_probe()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _exclusive("ingestion_scan", run_once),
        "interval",
        seconds=settings.scan_interval_seconds,
        id="ingestion_scan",
        coalesce=True,
        max_instances=1,
    )
    if settings.forecast_enabled:
        scheduler.add_job(
            _exclusive("forecast_training", _forecast_job),
            "interval",
            hours=settings.forecast_train_frequency_hours,
            id="forecast_training",
            coalesce=True,
            max_instances=1,
        )
        log.info(
            "forecast_training_scheduled",
            frequency_hours=settings.forecast_train_frequency_hours,
        )
    if settings.backtest_autopilot_enabled:
        scheduler.add_job(
            _exclusive("backtest_autopilot", _backtest_autopilot_job),
            "interval",
            hours=settings.backtest_autopilot_interval_hours,
            id="backtest_autopilot",
            coalesce=True,
            max_instances=1,
        )
        log.info(
            "backtest_autopilot_scheduled",
            interval_hours=settings.backtest_autopilot_interval_hours,
        )
    if settings.retention_autopilot_enabled and settings.archive_enabled:
        scheduler.add_job(
            _exclusive("retention_autopilot", _retention_job),
            "interval",
            hours=settings.retention_cadence_hours,
            id="retention_autopilot",
            coalesce=True,
            max_instances=1,
        )
        log.info(
            "retention_autopilot_scheduled",
            cadence_hours=settings.retention_cadence_hours,
        )
    if settings.parity_enabled and settings.archive_enabled:
        scheduler.add_job(
            _exclusive("lake_parity", _parity_job),
            "interval",
            hours=settings.parity_frequency_hours,
            id="lake_parity",
            coalesce=True,
            max_instances=1,
        )
        log.info(
            "parity_scheduled",
            frequency_hours=settings.parity_frequency_hours,
        )
    log.info("scheduler_started", interval_seconds=settings.scan_interval_seconds)
    scheduler.start()


if __name__ == "__main__":
    main()
