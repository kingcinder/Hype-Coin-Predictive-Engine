from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

from common.config import get_settings
from common.logging import get_logger
from forecast.engine import maybe_run_forecast
from ingestion.source_clients import ensure_background_probe
from ingestion.worker import run_once
from ops.retention import run_retention

log = get_logger(__name__)


def _retention_job() -> None:
    """Scheduled retention pass: compact + prune + lake-growth report."""
    from storage.database import SessionLocal

    with SessionLocal() as session:
        result = run_retention(session)
        session.commit()
    log.info("retention_autopilot_run", result=result)


def _forecast_job() -> None:
    """Cadence-gated forecast retraining and drift-baseline persistence."""
    result = maybe_run_forecast()
    if result.get("status") != "skipped":
        log.info("forecast_training_run", result=result)


def main() -> None:
    settings = get_settings()
    ensure_background_probe()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_once, "interval", seconds=settings.scan_interval_seconds, id="ingestion_scan"
    )
    if settings.forecast_enabled:
        scheduler.add_job(
            _forecast_job,
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
    if settings.retention_autopilot_enabled and settings.archive_enabled:
        scheduler.add_job(
            _retention_job,
            "interval",
            hours=settings.retention_cadence_hours,
            id="retention_autopilot",
        )
        log.info(
            "retention_autopilot_scheduled",
            cadence_hours=settings.retention_cadence_hours,
        )
    log.info("scheduler_started", interval_seconds=settings.scan_interval_seconds)
    scheduler.start()


if __name__ == "__main__":
    main()
