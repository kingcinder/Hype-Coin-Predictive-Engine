"""Retention autopilot: scheduled compaction + pruning with lake-growth reporting.

Runs the archive compactor (raw evidence -> partitioned Parquet) and the
pruner on a configurable cadence, then persists a ``RetentionRun`` with the
Parquet lake totals and the growth vs the previous pass, and records a
``component:lake`` health row so Feed Health shows how fast the evidence lake
is growing.

Driven three ways (pick one — they are idempotent and cadence-safe):

- **APScheduler** (docker profile): ``ingestion/scheduler.py`` registers a job
  on ``RETENTION_CADENCE_HOURS``.
- **Worker loop** (zero-container profile): ``python -m ingestion.worker
  --loop`` checks ``retention_due`` after each scan and runs the pass when the
  cadence has elapsed.
- **OS scheduler**: ``python -m ops.retention --once`` from a systemd timer
  (``deploy/systemd/serpent-retention.timer``) or a Windows Task Scheduler
  entry (``scripts/install_retention_task.ps1``).

A failed pass never kills the caller: it records ``component:lake`` health as
``red`` and returns ``{"error": ...}``, mirroring the archive compactor's
degradation contract.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from ops.archive import run_archive
from storage import models
from storage.repository import record_health

log = get_logger(__name__)


def lake_report(session: Session) -> dict[str, int]:
    """Current Parquet lake totals from the archive manifests."""
    row = session.execute(
        select(
            func.count(models.ArchiveManifest.id),
            func.coalesce(func.sum(models.ArchiveManifest.row_count), 0),
            func.coalesce(func.sum(models.ArchiveManifest.byte_size), 0),
        )
    ).one()
    return {
        "partitions": int(row[0]),
        "archived_rows": int(row[1]),
        "byte_size": int(row[2]),
    }


def _latest_run(session: Session) -> models.RetentionRun | None:
    return session.scalar(
        select(models.RetentionRun).order_by(models.RetentionRun.ts.desc()).limit(1)
    )


def run_retention(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    """Run one full retention pass: compact + prune + lake-growth report.

    Returns the run summary (totals, growth, compacted/pruned counts) or
    ``{"skipped": True}`` when the autopilot is disabled, or ``{"error": ...}``
    on failure — never raises.
    """
    settings = settings or get_settings()
    if not settings.retention_autopilot_enabled or not settings.archive_enabled:
        return {"skipped": True}
    decision_ts = ensure_utc(decision_ts or utc_now())
    started = monotonic()
    try:
        archive_result = run_archive(session, decision_ts=decision_ts, settings=settings)
        if "error" in archive_result:
            raise RuntimeError(str(archive_result["error"]))
        report = lake_report(session)
        previous = _latest_run(session)
        growth_bytes = report["byte_size"] - (previous.byte_size if previous else 0)
        growth_pct = (
            (growth_bytes / previous.byte_size * 100.0)
            if previous is not None and previous.byte_size
            else None
        )
        run = models.RetentionRun(
            ts=decision_ts,
            partitions=report["partitions"],
            archived_rows=report["archived_rows"],
            byte_size=report["byte_size"],
            compacted=int(archive_result.get("compacted", 0)),
            pruned=int(archive_result.get("pruned", 0)),
            growth_bytes=growth_bytes,
            growth_pct=round(growth_pct, 2) if growth_pct is not None else None,
            duration_sec=round(monotonic() - started, 3),
        )
        session.add(run)
        session.flush()
        record_health(
            session,
            component="lake",
            state="ok",
            message=(
                f"partitions={run.partitions} rows={run.archived_rows} "
                f"bytes={run.byte_size} growth_bytes={run.growth_bytes} "
                f"growth_pct={run.growth_pct if run.growth_pct is not None else 0.0}% "
                f"compacted={run.compacted} pruned={run.pruned}"
            ),
        )
        return {
            "status": "ok",
            "partitions": run.partitions,
            "archived_rows": run.archived_rows,
            "byte_size": run.byte_size,
            "growth_bytes": run.growth_bytes,
            "growth_pct": run.growth_pct,
            "compacted": run.compacted,
            "pruned": run.pruned,
            "duration_sec": run.duration_sec,
        }
    except Exception as exc:  # noqa: BLE001 - retention failure must never kill the caller.
        record_health(
            session,
            component="lake",
            state="red",
            message=str(exc),
            error_count=1,
        )
        log.warning("retention_autopilot_failed", error=str(exc))
        return {"error": str(exc)}


def retention_due(
    session: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> bool:
    """True when the cadence since the last retention pass has elapsed.

    A disabled autopilot is never due; with no recorded pass the pass is due
    (first run). The worker loop calls this after each scan so the zero-container
    profile self-schedules without APScheduler.
    """
    settings = settings or get_settings()
    if not settings.retention_autopilot_enabled or not settings.archive_enabled:
        return False
    now = ensure_utc(now or utc_now())
    latest = _latest_run(session)
    if latest is None:
        return True
    return now - ensure_utc(latest.ts) >= timedelta(
        hours=settings.retention_cadence_hours
    )


def maybe_run_retention() -> dict[str, object]:
    """Cadence-gated retention pass for the zero-container worker loop."""
    from storage.database import SessionLocal

    with SessionLocal() as session:
        if not retention_due(session):
            return {"skipped": True}
        result = run_retention(session)
        session.commit()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serpent Circle retention autopilot: compaction + pruning + lake growth"
    )
    parser.add_argument(
        "--once", action="store_true", help="run one retention pass and exit"
    )
    parser.add_argument(
        "--check-due",
        action="store_true",
        help="exit 0 when a retention pass is due, 1 otherwise",
    )
    args = parser.parse_args()

    from storage.database import SessionLocal

    if args.check_due:
        with SessionLocal() as session:
            due = retention_due(session)
        print("due" if due else "not-due")
        raise SystemExit(0 if due else 1)

    if args.once:
        settings = get_settings()
        print(
            f"retention autopilot enabled={settings.retention_autopilot_enabled} "
            f"cadence_hours={settings.retention_cadence_hours} "
            f"backend={settings.archive_backend}"
        )
        with SessionLocal() as session:
            result = run_retention(session)
            session.commit()
        print(json.dumps(result, default=str))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
