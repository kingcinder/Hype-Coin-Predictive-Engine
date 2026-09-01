from __future__ import annotations

import argparse
import time

from common.config import get_settings
from common.logging import get_logger
from forecast.engine import maybe_run_forecast
from ingestion.service import IngestionService, backoff_sleep_seconds
from ingestion.source_clients import ensure_background_probe
from ops.parity import maybe_run_parity
from ops.retention import check_lake_freshness, maybe_run_retention
from storage.database import SessionLocal

log = get_logger(__name__)


def run_once() -> dict[str, object]:
    service = IngestionService()
    with SessionLocal() as session:
        # Pre-scan lake freshness check: when the retention cadence has elapsed
        # since the last pass, surface the stale lake as yellow Feed Health
        # (same gate as `ops.retention --check-due`). Committed here so the
        # yellow row survives even if the scan itself fails and rolls back.
        freshness = check_lake_freshness(session)
        session.commit()
        return service.run_once(session, pre_scan_health={"lake": freshness})


def main() -> None:
    parser = argparse.ArgumentParser(description="Serpent Circle ingestion worker")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--loop", action="store_true", help="run forever")
    args = parser.parse_args()
    settings = get_settings()

    if args.once or not args.loop:
        result = run_once()
        log.info("worker_once_complete", result=result)
        return

    # Single-writer guard: refuse to start if another process already owns the
    # SQLite file (a second writer causes "database is locked" loop wedges).
    from storage.database import acquire_sqlite_writer_lock

    _db_lock: int | None = None
    try:
        _db_lock = acquire_sqlite_writer_lock(settings)
    except RuntimeError as exc:  # noqa: BLE001
        log.critical("worker_sqlite_writer_conflict", error=str(exc))
        print(str(exc))
        raise SystemExit(1) from None

    ensure_background_probe()
    iteration = 0
    while True:
        iteration += 1
        result = run_once()
        log.info("worker_loop_complete", result=result)
        # The scan performs the initial/due pass before scoring; this second
        # cadence gate also lets a long-running worker retrain independently of
        # scan implementation details.
        forecast = maybe_run_forecast()
        if forecast.get("status") != "skipped":
            log.info("forecast_training_complete", result=forecast)
        # Retention autopilot: run the compaction + pruning + lake-growth pass
        # when the configured cadence has elapsed since the last pass. The scan
        # never touches the archive; this cadence fully owns compaction on the
        # per-partition schedule and reports the lake in Feed Health.
        retention = maybe_run_retention()
        if not retention.get("skipped"):
            log.info("retention_autopilot_complete", result=retention)
        # Lake-vs-SQL parity CI: compare the DuckDB lake read path against the
        # live SQL path on the daily cadence and page a mismatch via ntfy.
        parity = maybe_run_parity()
        if not parity.get("skipped"):
            log.info("parity_check_complete", result=parity)
        time.sleep(backoff_sleep_seconds(iteration, settings.scan_interval_seconds))


if __name__ == "__main__":
    main()
