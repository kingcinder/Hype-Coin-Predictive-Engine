"""Retention autopilot: scheduled compaction + pruning with lake-growth reporting.

Runs the archive compactor (raw evidence -> partitioned Parquet) and the
pruner on a configurable cadence, then persists a ``RetentionRun`` with the
Parquet lake totals and the growth vs the previous pass, and records a
``component:lake`` health row so Feed Health shows how fast the evidence lake
is growing.

Compaction runs on a **per-partition schedule**: each pass computes the
partitions whose evidence has aged past ``ARCHIVE_COMPACT_AFTER_HOURS`` and
compacts exactly those, so a pass with nothing due does zero compaction work.
The ingestion scan never touches the archive — this cadence fully owns
compaction.

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
from collections.abc import Sequence
from datetime import datetime, timedelta
from time import monotonic
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from ops.archive import due_partitions, run_archive
from ops.notifier import notify_lake_budget
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
        # Per-partition schedule: compact exactly the partitions whose evidence
        # has aged past ARCHIVE_COMPACT_AFTER_HOURS. A pass with nothing due
        # does zero compaction work (only pruning runs). The ingestion scan no
        # longer touches the archive — this cadence fully owns compaction.
        due = due_partitions(session, decision_ts, settings)
        archive_result = run_archive(
            session,
            decision_ts=decision_ts,
            settings=settings,
            partition_filter=set(due),
        )
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
                f"compacted={run.compacted} pruned={run.pruned} "
                f"due_partitions={len(due)}"
            ),
        )
        # Retention budget alert: warn via ntfy when the projected lake growth
        # would fill the capacity cap within RETENTION_BUDGET_ALERT_DAYS.
        history = session.scalars(
            select(models.RetentionRun)
            .order_by(models.RetentionRun.ts.desc())
            .limit(60)
        ).all()
        budget = check_lake_budget(
            session, history=history, now=decision_ts, settings=settings
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
            "due_partitions": len(due),
            "lake_budget": budget,
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


# Projection horizon cap: 10 years. Beyond this the linear extrapolation is
# treated as "no practical disk-full horizon" (projected_full_at=None).
MAX_PROJECTION_HOURS = 24 * 365 * 10


class LakeGrowthProjection(TypedDict):
    """Disk-full projection computed from retention-pass history."""

    growth_rate_bytes_per_hour: float
    projected_full_at: datetime | None
    days_to_full: float | None
    pct_full: float
    sample_runs: int


def project_lake_growth(
    runs: Sequence[models.RetentionRun],
    *,
    max_bytes: int,
) -> LakeGrowthProjection:
    """Project the disk-full horizon from retention-pass history.

    Fits a linear regression of ``byte_size`` on elapsed hours and extrapolates
    to ``max_bytes``. Returns the growth rate (bytes/hour), the projected
    full-timestamp, days remaining, and how full the lake currently is. A lake
    with fewer than two passes or a flat/shrinking trend yields
    ``projected_full_at=None`` and ``days_to_full=None``.
    """
    ordered = sorted(runs, key=lambda r: ensure_utc(r.ts))
    latest = ordered[-1] if ordered else None
    base: LakeGrowthProjection = {
        "growth_rate_bytes_per_hour": 0.0,
        "projected_full_at": None,
        "days_to_full": None,
        "pct_full": 0.0,
        "sample_runs": len(ordered),
    }
    if latest is None or max_bytes <= 0:
        return base
    base["pct_full"] = round(min(100.0, latest.byte_size / max_bytes * 100.0), 2)
    if len(ordered) < 2:
        return base
    t0 = ensure_utc(ordered[0].ts)
    hours = [(ensure_utc(r.ts) - t0).total_seconds() / 3600.0 for r in ordered]
    mean_h = sum(hours) / len(hours)
    mean_b = sum(r.byte_size for r in ordered) / len(ordered)
    numerator = sum(
        (h - mean_h) * (r.byte_size - mean_b)
        for h, r in zip(hours, ordered, strict=True)
    )
    denominator = sum((h - mean_h) ** 2 for h in hours)
    if denominator <= 0:
        return base
    slope = numerator / denominator
    if slope <= 0:
        return base
    base["growth_rate_bytes_per_hour"] = round(slope, 2)
    remaining = max_bytes - latest.byte_size
    if remaining <= 0:
        base["projected_full_at"] = ensure_utc(latest.ts)
        base["days_to_full"] = 0.0
        return base
    hours_to_full = remaining / slope
    # Cap the extrapolation at a practical 10-year horizon: beyond that the
    # linear fit is meaningless (and for tiny growth rates against a large
    # capacity cap the datetime arithmetic would overflow). A far-horizon lake
    # reports "no practical disk-full date" rather than a nonsense timestamp.
    if hours_to_full > MAX_PROJECTION_HOURS:
        return base
    base["projected_full_at"] = ensure_utc(latest.ts) + timedelta(hours=hours_to_full)
    base["days_to_full"] = round(hours_to_full / 24.0, 2)
    return base


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


def _budget_alert_due(
    session: Session,
    now: datetime,
    cooldown_hours: float,
) -> bool:
    """True when the last lake-budget alert is older than the cooldown (or
    there is none), so repeated passes do not spam the same warning."""
    last = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "lake_budget")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    if last is None or last.ts is None:
        return True
    return now - ensure_utc(last.ts) >= timedelta(hours=cooldown_hours)


def check_lake_budget(
    session: Session,
    *,
    history: Sequence[models.RetentionRun],
    now: datetime,
    settings: Settings,
) -> dict[str, object]:
    """Evaluate the retention budget and fire the ntfy warning when needed.

    Projects the disk-full horizon from ``history`` against
    ``ARCHIVE_LAKE_MAX_BYTES``; when the projected fill lands within
    ``RETENTION_BUDGET_ALERT_DAYS``, records a ``component:lake_budget``
    health row (yellow, or red when already at/over capacity) and pushes an
    ntfy warning — at most once per
    ``RETENTION_BUDGET_ALERT_COOLDOWN_HOURS``. Returns the budget verdict;
    never raises.
    """
    projection = project_lake_growth(history, max_bytes=settings.archive_lake_max_bytes)
    days = projection["days_to_full"]
    if days is None:
        return {"alert": False, "days_to_full": None}
    if days > settings.retention_budget_alert_days:
        return {"alert": False, "days_to_full": days}
    state = "red" if days <= 0 else "yellow"
    message = (
        f"lake budget: projected full in {days:.1f} days "
        f"({projection['pct_full']:.1f}% full, "
        f"{projection['growth_rate_bytes_per_hour']:,.0f} B/h growth, "
        f"cap {settings.archive_lake_max_bytes:,} B)"
    )
    push_due = _budget_alert_due(
        session, now, settings.retention_budget_alert_cooldown_hours
    )
    record_health(
        session,
        component="lake_budget",
        state=state,
        message=message,
        ts=now,
    )
    pushed = False
    if push_due:
        pushed = notify_lake_budget(
            days_to_full=float(days),
            pct_full=float(projection["pct_full"]),
            growth_rate_bytes_per_hour=float(projection["growth_rate_bytes_per_hour"]),
            max_bytes=settings.archive_lake_max_bytes,
            settings=settings,
        )
    return {
        "alert": True,
        "state": state,
        "days_to_full": days,
        "pushed": pushed,
    }


def check_lake_freshness(
    session: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    """Pre-scan lake freshness check: surface a stale lake as yellow health.

    Uses the same gate as ``ops.retention --check-due``: when a retention
    pass has been recorded but the cadence since it has elapsed, records
    ``component:lake`` health as ``yellow`` so Feed Health flags the stale
    lake before the scan runs. Fresh lakes, disabled autopilots, and lakes
    with no recorded pass yet record nothing (the last pass already wrote
    the ``ok`` row, and a brand-new deployment has no lake to be stale).
    The worker calls this before each scan; the post-scan retention pass
    writes ``ok`` when it runs.
    """
    settings = settings or get_settings()
    if not settings.retention_autopilot_enabled or not settings.archive_enabled:
        return {"fresh": True, "recorded": False}
    now = ensure_utc(now or utc_now())
    latest = _latest_run(session)
    if latest is None:
        return {"fresh": True, "recorded": False}
    if not retention_due(session, now=now, settings=settings):
        return {"fresh": True, "recorded": False}
    stale_hours = (now - ensure_utc(latest.ts)).total_seconds() / 3600.0
    record_health(
        session,
        component="lake",
        state="yellow",
        message=(
            f"lake stale: last retention pass {stale_hours:.1f}h ago "
            f"(cadence {settings.retention_cadence_hours}h)"
        ),
    )
    return {"fresh": False, "recorded": True, "stale_hours": round(stale_hours, 2)}


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
