"""Scheduled backtest autopilot: walk-forward backtest with drift detection.

Runs a full walk-forward backtest over a trailing lookback window every 24h
(via ``ingestion/scheduler.py``), then compares headline metrics — precision@10,
median forward return, and collapse rate — against the previous completed run.
Meaningful degradation records a ``backtest_drift`` SystemHealth component so
the Health & Diagnostics view and ``/health`` surface the drift, and a
``backtest_drift`` Alert is opened when the drift is severe enough to page an
operator (ignition-style alerting reuses the notifier path).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backtest.runner import run_backtest
from common.config import get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

DRIFT_COMPONENT = "backtest_drift"


def _metrics_for_run(session: Session, run_id: int) -> dict[str, float]:
    """Map metric_name -> value for a completed backtest run."""
    rows = session.scalars(
        select(models.BacktestResult).where(models.BacktestResult.run_id == run_id)
    ).all()
    return {row.metric_name: row.metric_value for row in rows}


def _previous_completed_run(session: Session, before_run_id: int) -> models.BacktestRun | None:
    """The most recent completed run before the given one (the drift baseline)."""
    return session.scalar(
        select(models.BacktestRun)
        .where(
            models.BacktestRun.id != before_run_id,
            models.BacktestRun.status == "completed",
        )
        .order_by(desc(models.BacktestRun.started_at))
        .limit(1)
    )


def _compute_drift(
    metrics: dict[str, float],
    previous: dict[str, float],
    *,
    precision_margin: float,
    return_floor: float,
    collapse_rise: float,
) -> tuple[str, list[str]]:
    """Compare current vs previous-run metrics and return (state, reasons).

    ``state`` is ``red`` (page an operator), ``yellow`` (degraded but
    recoverable), or ``ok``.  A missing baseline (first autopilot run) is
    ``ok`` — there is nothing to drift against yet.
    """
    if not previous:
        return "ok", []

    reasons: list[str] = []
    prev_precision = previous.get("precision_at_10")
    precision = metrics.get("precision_at_10")
    if (
        prev_precision is not None
        and precision is not None
        and prev_precision - precision >= precision_margin
    ):
        reasons.append(f"precision@10 {prev_precision:.1%} -> {precision:.1%}")

    prev_return = previous.get("median_forward_return")
    median_return = metrics.get("median_forward_return")
    if prev_return is not None and median_return is not None and median_return < return_floor:
        reasons.append(
            f"median forward return {median_return:.1f}% below floor {return_floor:.1f}%"
        )

    prev_collapse = previous.get("collapse_rate")
    collapse_rate = metrics.get("collapse_rate")
    if (
        prev_collapse is not None
        and collapse_rate is not None
        and collapse_rate - prev_collapse >= collapse_rise
    ):
        reasons.append(f"collapse rate {prev_collapse:.1%} -> {collapse_rate:.1%}")

    if not reasons:
        return "ok", []
    # Two or more degraded headline metrics, or a return floor breach on its
    # own, is a red drift.  A single precision or collapse slip is yellow.
    severe = len(reasons) >= 2 or any("floor" in reason for reason in reasons)
    return ("red" if severe else "yellow"), reasons


def run_backtest_autopilot(session: Session, *, decision_ts=None) -> dict[str, object]:
    """Run the scheduled backtest and record drift vs the previous run.

    Returns a summary dict with the new run id, the drift state/reasons, and
    the headline metrics so the caller can log and the GUI can display it.
    """
    settings = get_settings()
    decision_ts = ensure_utc(decision_ts or utc_now())
    start = decision_ts - timedelta(days=settings.backtest_lookback_days)

    try:
        run = run_backtest(
            session,
            start=start,
            end=decision_ts,
            top_k=settings.backtest_autopilot_top_k,
            forward_hours=settings.backtest_autopilot_forward_hours,
            feature_source=settings.backtest_autopilot_feature_source,
        )
    except Exception as exc:  # noqa: BLE001 - a failed backtest must surface as health, not vanish.
        log.error("backtest_autopilot_failed", error=str(exc))
        record_health(
            session,
            component=DRIFT_COMPONENT,
            state="red",
            message=f"backtest autopilot failed: {exc}",
            ts=decision_ts,
            error_count=1,
        )
        # Commit the health record before re-raising: the scheduler only
        # commits on the success path, so an un-committed row would be rolled
        # back when the session closes and the failure would vanish again.
        session.commit()
        raise
    session.flush()
    metrics = _metrics_for_run(session, run.id)

    previous_run = _previous_completed_run(session, run.id)
    previous_metrics = _metrics_for_run(session, previous_run.id) if previous_run else {}

    state, reasons = _compute_drift(
        metrics,
        previous_metrics,
        precision_margin=settings.backtest_drift_precision_margin,
        return_floor=settings.backtest_drift_return_floor,
        collapse_rise=settings.backtest_drift_collapse_rise,
    )

    message = (
        f"backtest run={run.id} precision@10={metrics.get('precision_at_10', 0):.1%} "
        f"median_fwd_return={metrics.get('median_forward_return', 0):.1f}% "
        f"collapse_rate={metrics.get('collapse_rate', 0):.1%} "
        f"scam_avoidance={metrics.get('scam_avoidance_rate', 0):.1%} "
        f"baseline_run={previous_run.id if previous_run else 'none'}"
    )
    if reasons:
        message += f" | drift: {'; '.join(reasons)}"
    else:
        message += " | no drift"

    record_health(
        session,
        component=DRIFT_COMPONENT,
        state=state,
        message=message,
        ts=decision_ts,
    )
    session.flush()

    # Open an operator-facing alert when drift is red so the notifier path
    # pages it (same alert_type machinery as ignition/lifecycle alerts).
    if state == "red":
        _open_drift_alert(session, run_id=run.id, reasons=reasons, decision_ts=decision_ts)
        session.flush()

    log.info(
        "backtest_autopilot_run",
        run_id=run.id,
        state=state,
        reasons=reasons,
        precision_at_10=metrics.get("precision_at_10", 0.0),
        median_forward_return=metrics.get("median_forward_return", 0.0),
    )
    return {
        "run_id": run.id,
        "state": state,
        "reasons": reasons,
        "metrics": metrics,
        "baseline_run_id": previous_run.id if previous_run else None,
    }


def _open_drift_alert(
    session: Session,
    *,
    run_id: int,
    reasons: list[str],
    decision_ts,
) -> None:
    """Open a backtest_drift alert for the most recent red drift run.

    Deduplicates: only one open backtest_drift alert is kept at a time, so
    repeated red runs re-open the same alert instead of stacking noise.
    """
    existing = session.scalar(
        select(models.Alert).where(
            models.Alert.alert_type == "backtest_drift",
            models.Alert.state == "open",
        )
    )
    if existing is not None:
        existing.score_snapshot_ref = f"backtest:{run_id}"
        existing.message = "Backtest drift: " + "; ".join(reasons)
        return
    asset_id = _first_asset_id(session)
    if asset_id is None:
        # No assets in the DB yet — health row is the only surface for drift.
        return
    session.add(
        models.Alert(
            asset_id=asset_id,
            alert_type="backtest_drift",
            threshold_version="autopilot-v1",
            score_snapshot_ref=f"backtest:{run_id}",
            state="open",
            message="Backtest drift: " + "; ".join(reasons),
            created_at=decision_ts,
        )
    )


def _first_asset_id(session: Session) -> int | None:
    """Anchor asset for the drift alert; any asset satisfies the FK.

    Returns None when the universe is empty so the alert can be skipped
    gracefully (health still records the drift).
    """
    asset = session.scalar(select(models.Asset.id).limit(1))
    return int(asset) if asset is not None else None
