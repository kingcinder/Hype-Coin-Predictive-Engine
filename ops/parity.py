"""Lake-vs-SQL parity CI: daily comparison of the DuckDB lake read path
against the live SQL path over the full archived lake.

``features/lake.py`` reconstructs the lake-covered feature block (market /
liquidity series plus the on-chain holder and contract-flag features) from
the archived Parquet evidence; ``tests/test_lake_features.py`` proves the two
read paths agree on a seeded fixture. This module runs the same comparison
against the *production* lake once per day and pages any divergence via ntfy,
so a payload-shape change, an extraction-rule drift, or a DuckDB behavior
change that would silently skew lake-replayed backtests is surfaced instead
of quietly corrupting results.

The comparison decision time is floored to an hour and clamped to a horizon
that guarantees every piece of evidence at that time is inside the archived
lake (older than ``ARCHIVE_COMPACT_AFTER_HOURS`` +
``RETENTION_CADENCE_HOURS``), so the SQL path and the lake path are provably
reading the same observations.

Driven three ways (pick one — they are idempotent and cadence-safe):

- **APScheduler** (docker profile): ``ingestion/scheduler.py`` registers a job
  on ``PARITY_FREQUENCY_HOURS``.
- **Worker loop** (zero-container profile): ``python -m ingestion.worker
  --loop`` runs the cadence-gated :func:`maybe_run_parity` after each scan.
- **OS scheduler**: ``python -m ops.parity --once`` from a systemd timer or a
  Windows Task Scheduler entry (``--strict`` exits non-zero when a mismatch
  is detected, for CI integration).

A failed run never kills the caller: it records ``component:parity`` health
as ``red`` and returns ``{"error": ...}``, mirroring the retention
autopilot's degradation contract.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc, floor_to_hour, utc_now
from features.factory import FeatureFactory
from features.lake import LAKE_FEATURE_NAMES, LakeFeatureFactory
from ops.notifier import notify_parity_mismatch
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

# SystemHealth component used both as the run marker (cadence gate) and the
# alert-cooldown marker (the last red row).
PARITY_HEALTH_COMPONENT = "parity"


@dataclass(frozen=True)
class ParityMismatch:
    """One lake-vs-SQL divergence for a single (asset, feature)."""

    asset_id: int
    symbol: str
    feature_name: str
    sql_value: float | None
    lake_value: float | None
    sql_missing: bool
    lake_missing: bool


def _diverged(a: float, b: float, tolerance: float) -> bool:
    """Relative divergence beyond tolerance (scale-aware, like the parity test)."""
    scale = max(1.0, abs(a), abs(b))
    return abs(a - b) / scale > tolerance


def _format_mismatch(mismatch: ParityMismatch) -> str:
    sql_side = "missing" if mismatch.sql_missing else f"{mismatch.sql_value}"
    lake_side = "missing" if mismatch.lake_missing else f"{mismatch.lake_value}"
    return f"{mismatch.symbol} [{mismatch.feature_name}]: sql={sql_side} lake={lake_side}"


def compare_asset(
    session: Session,
    asset: models.Asset,
    *,
    decision_ts: datetime,
    tolerance: float,
    settings: Settings | None = None,
) -> list[ParityMismatch]:
    """Build the same asset's features through both read paths and return the
    divergences among the lake-covered names.

    SQL path: ``FeatureFactory.build_for_asset`` over the live normalized
    tables. Lake path: ``LakeFeatureFactory.build_for_asset`` over the
    archived Parquet evidence (DuckDB). Only the names in
    ``LAKE_FEATURE_NAMES`` are compared — the other features need SQL-side
    state (narratives, forecasts, lifecycle) by design.
    """
    sql_values = {
        value.name: value
        for value in FeatureFactory().build_for_asset(session, asset, decision_ts)
    }
    lake_values = LakeFeatureFactory(settings=settings or get_settings()).build_for_asset(
        asset_address=asset.address, decision_ts=decision_ts
    )
    mismatches: list[ParityMismatch] = []
    for name in LAKE_FEATURE_NAMES:
        sql_feature = sql_values[name]
        lake_feature = lake_values[name]
        if sql_feature.missing != lake_feature.missing:
            mismatches.append(
                ParityMismatch(
                    asset_id=asset.id,
                    symbol=asset.symbol,
                    feature_name=name,
                    sql_value=sql_feature.value,
                    lake_value=lake_feature.value,
                    sql_missing=sql_feature.missing,
                    lake_missing=lake_feature.missing,
                )
            )
            continue
        if sql_feature.missing:
            continue
        if _diverged(sql_feature.value, lake_feature.value, tolerance):
            mismatches.append(
                ParityMismatch(
                    asset_id=asset.id,
                    symbol=asset.symbol,
                    feature_name=name,
                    sql_value=sql_feature.value,
                    lake_value=lake_feature.value,
                    sql_missing=False,
                    lake_missing=False,
                )
            )
    return mismatches


def parity_decision_ts(settings: Settings, now: datetime) -> datetime:
    """Decision time for the comparison: a floored hour far enough in the past
    that every piece of evidence at that time is provably archived."""
    horizon = max(
        settings.parity_compare_hours_ago,
        settings.archive_compact_after_hours + settings.retention_cadence_hours + 1.0,
    )
    return floor_to_hour(now) - timedelta(hours=horizon)


def _parity_push_due(session: Session, now: datetime, cooldown_hours: float) -> bool:
    """True when the last red parity row is older than the cooldown (or there
    is none), so a broken lake cannot spam the same page every run."""
    last_red = session.scalar(
        select(models.SystemHealth)
        .where(
            models.SystemHealth.component == PARITY_HEALTH_COMPONENT,
            models.SystemHealth.state == "red",
        )
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    if last_red is None or last_red.ts is None:
        return True
    return now - ensure_utc(last_red.ts) >= timedelta(hours=cooldown_hours)


def parity_due(
    session: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> bool:
    """True when the parity cadence since the last run has elapsed.

    The last ``component:parity`` health row is the run marker: with no run
    yet the check is due (first run), and a disabled parity check is never
    due. The worker loop calls this after each scan so the zero-container
    profile self-schedules without APScheduler.
    """
    settings = settings or get_settings()
    if not settings.parity_enabled or not settings.archive_enabled:
        return False
    now = ensure_utc(now or utc_now())
    last = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == PARITY_HEALTH_COMPONENT)
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    if last is None or last.ts is None:
        return True
    return now - ensure_utc(last.ts) >= timedelta(hours=settings.parity_frequency_hours)


def run_parity(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    """Run one lake-vs-SQL parity comparison over the archived lake.

    Compares the SQL and DuckDB lake read paths for every asset (or the first
    ``PARITY_MAX_ASSETS``), records ``component:parity`` health (``ok`` /
    ``yellow`` / ``red`` by mismatch count), and pages a mismatch via ntfy at
    most once per ``PARITY_ALERT_COOLDOWN_HOURS``. Returns the run summary or
    ``{"skipped": True}`` when disabled, or ``{"error": ...}`` on failure —
    never raises.
    """
    settings = settings or get_settings()
    if not settings.parity_enabled or not settings.archive_enabled:
        return {"skipped": True}
    if decision_ts is not None:
        decision_ts = floor_to_hour(ensure_utc(decision_ts))
    else:
        decision_ts = parity_decision_ts(settings, utc_now())
    started = monotonic()
    try:
        stmt = select(models.Asset).order_by(models.Asset.id)
        if settings.parity_max_assets > 0:
            stmt = stmt.limit(settings.parity_max_assets)
        assets = session.scalars(stmt).all()
        mismatches: list[ParityMismatch] = []
        errors = 0
        for asset in assets:
            try:
                mismatches.extend(
                    compare_asset(
                        session,
                        asset,
                        decision_ts=decision_ts,
                        tolerance=settings.parity_tolerance,
                        settings=settings,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one asset must not kill the pass.
                errors += 1
                log.warning("parity_asset_failed", asset_id=asset.id, error=str(exc))
        mismatch_count = len(mismatches)
        state = "ok"
        if mismatch_count > 0:
            state = (
                "red"
                if mismatch_count >= settings.parity_alert_threshold
                else "yellow"
            )
        examples = [_format_mismatch(mismatch) for mismatch in mismatches[:5]]
        # Evaluate the push cooldown BEFORE recording this run's health row,
        # otherwise the gate would always see the just-written red row and
        # suppress the page (the lake-budget check does the same).
        push_due = (
            mismatch_count >= settings.parity_alert_threshold
            and _parity_push_due(session, utc_now(), settings.parity_alert_cooldown_hours)
        )
        record_health(
            session,
            component=PARITY_HEALTH_COMPONENT,
            state=state,
            message=(
                f"lake-vs-SQL parity: {mismatch_count} mismatches across "
                f"{len(assets)} assets at decision {decision_ts.isoformat()}; "
                f"tolerance={settings.parity_tolerance}"
            ),
            error_count=errors,
        )
        pushed = False
        if push_due:
            pushed = notify_parity_mismatch(
                mismatch_count=mismatch_count,
                compared_assets=len(assets),
                decision_ts=decision_ts,
                examples=examples,
                settings=settings,
            )
        return {
            "status": "ok" if state == "ok" else state,
            "compared_assets": len(assets),
            "mismatches": mismatch_count,
            "errors": errors,
            "decision_ts": decision_ts.isoformat(),
            "pushed": pushed,
            "duration_sec": round(monotonic() - started, 3),
            "examples": examples,
        }
    except Exception as exc:  # noqa: BLE001 - parity failure must never kill the caller.
        record_health(
            session,
            component=PARITY_HEALTH_COMPONENT,
            state="red",
            message=str(exc),
            error_count=1,
        )
        log.warning("parity_check_failed", error=str(exc))
        return {"error": str(exc)}


def maybe_run_parity() -> dict[str, object]:
    """Cadence-gated parity check for the zero-container worker loop."""
    from storage.database import SessionLocal

    with SessionLocal() as session:
        if not parity_due(session):
            return {"skipped": True}
        result = run_parity(session)
        session.commit()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serpent Circle lake-vs-SQL parity CI: compare the DuckDB "
        "lake read path against the live SQL path and page mismatches via ntfy"
    )
    parser.add_argument(
        "--once", action="store_true", help="run one parity check and exit"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when a mismatch is detected (CI integration)",
    )
    args = parser.parse_args()

    from storage.database import SessionLocal

    if args.once:
        settings = get_settings()
        print(
            f"parity enabled={settings.parity_enabled} archive={settings.archive_backend} "
            f"frequency_hours={settings.parity_frequency_hours} "
            f"compare_hours_ago={settings.parity_compare_hours_ago} "
            f"tolerance={settings.parity_tolerance}"
        )
        with SessionLocal() as session:
            result = run_parity(session)
            session.commit()
        print(json.dumps(result, default=str))
        if (
            args.strict
            and result.get("status") in ("red", "yellow")
        ):
            raise SystemExit(1)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
