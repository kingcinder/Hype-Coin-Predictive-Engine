"""Risk Outcome Tracker — records actual outcomes for flagged tokens.

After each scoring pass, this module looks up what happened to tokens
that were previously flagged at each risk band, computing precision
(recall, hit rate) for each band.  This data feeds the adaptive
calibrator so risk thresholds learn from real market outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import RiskBand
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)


@dataclass
class BandOutcome:
    """Outcome stats for one risk band over the observation window."""

    band: RiskBand
    total_flagged: int = 0
    collapsed: int = 0
    rugged: int = 0
    survived: int = 0
    unknown: int = 0
    precision: float = 0.0  # fraction that actually collapsed/rugged
    recall: float = 0.0  # fraction of all collapse events caught by this band


@dataclass
class RiskOutcomeReport:
    """Aggregated outcome report across all bands."""

    window_hours: float
    observation_ts: datetime
    bands: dict[str, BandOutcome] = field(default_factory=dict)
    overall_precision: float = 0.0
    total_flagged: int = 0
    total_collapsed: int = 0
    # Informational only: the health state is age-based, not growth-based.
    unresolved_grew: bool = False
    # Predictions past their window that still have no established outcome
    # (evaluated_at IS NULL). A growing count means withheld labels are
    # accumulating instead of being resolved.
    unresolved: int = 0
    # ML-specific band outcomes, keyed by the band the ML scorer predicted
    # (from RiskOutcome.details["ml_risk_band"]).  These feed the calibrator's
    # ML probability thresholds so the ML scorer calibrates independently of
    # the rule engine.
    ml_bands: dict[str, BandOutcome] = field(default_factory=dict)
    ml_total_flagged: int = 0
    ml_total_collapsed: int = 0


#: Lifecycle phases that establish an outcome.  A phase outside both sets at
#: window end (including no event at all) cannot label a prediction.
_COLLAPSE_PHASES = frozenset({"collapse", "dead", "rugged"})
_SURVIVAL_PHASES = frozenset({"survivor", "parabolic", "saturation"})

#: ``SystemHealth`` component carrying the unresolved-label backlog, so
#: withheld outcomes are visible in ``/health`` without grepping logs.
UNRESOLVED_OUTCOME_COMPONENT = "outcome_labels"

#: Bounded label version for outcome tracking (v1_unbounded → v2_bounded)
LABEL_VERSION = "v2_bounded"


def _lifecycle_phase_as_of(session: Session, asset_id: int, as_of: datetime) -> str | None:
    """Return the most recent lifecycle phase at or before ``as_of``.

    The bound is the point: a label for a prediction scored at ``T0`` may only
    see events inside its observation window ``[T0, T0 + window]``.  An
    unbounded "latest" lookup would label a Day-0 prediction with a collapse
    that happened after the window ended.
    """
    event = session.scalar(
        select(models.LifecycleEvent)
        .where(
            models.LifecycleEvent.asset_id == asset_id,
            models.LifecycleEvent.ts <= ensure_utc(as_of),
        )
        .order_by(models.LifecycleEvent.ts.desc())
        .limit(1)
    )
    return event.phase if event else None


def _price_change_since(
    session: Session,
    asset_id: int,
    since: datetime,
    *,
    until: datetime,
) -> float | None:
    """Return the price change from `since` to the last snapshot at/before `until`.

    ``until`` is the observation window end: a price snapshot after it must not
    stand in for the price at window end.
    """
    # Use the same pair lookup pattern as the rest of the codebase: first pair
    # matching base_asset_id with a USDC-like quote.  Fall back to any pair.
    pair = session.scalar(
        select(models.Pair.id)
        .join(models.Asset, models.Asset.id == models.Pair.quote_asset_id)
        .where(
            models.Pair.base_asset_id == asset_id,
            models.Asset.symbol.in_(("USDC", "USDT", "USD")),
        )
        .limit(1)
    )
    if pair is None:
        pair = session.scalar(
            select(models.Pair.id).where(models.Pair.base_asset_id == asset_id).limit(1)
        )
    if pair is None:
        return None
    entry = session.scalar(
        select(models.MarketSnapshot.price_usd)
        .where(
            models.MarketSnapshot.pair_id == pair,
            models.MarketSnapshot.ts <= since,
            models.MarketSnapshot.price_usd.is_not(None),
            models.MarketSnapshot.price_usd > 0,
        )
        .order_by(models.MarketSnapshot.ts.desc())
        .limit(1)
    )
    latest = session.scalar(
        select(models.MarketSnapshot.price_usd)
        .where(
            models.MarketSnapshot.pair_id == pair,
            models.MarketSnapshot.ts > since,
            models.MarketSnapshot.ts <= ensure_utc(until),
            models.MarketSnapshot.price_usd.is_not(None),
            models.MarketSnapshot.price_usd > 0,
        )
        .order_by(models.MarketSnapshot.ts.desc())
        .limit(1)
    )
    if entry is None or latest is None or entry <= 0:
        return None
    return (latest - entry) / entry


def record_risk_outcome(
    session: Session,
    *,
    asset_id: int,
    risk_band: str,
    score_id: int,
    decision_ts: datetime,
    ml_risk_band: str | None = None,
    heuristic_band: str | None = None,
) -> None:
    """Record a single risk outcome observation.

    Called after scoring to snapshot the token's state at scoring time
    so it can be evaluated later when the forward window elapses.

    ``ml_risk_band`` is the ML-specific risk band prediction (from
    ``collapse_probability_24h``).  When provided, the ML scorer gets
    independent feedback instead of duplicating the rule scorer's band.

    ``heuristic_band`` is the heuristic-layer risk band prediction (from the
    crawler ignition/signal score).  When provided, the heuristic scorer gets
    its own independent feedback, so all three ensemble layers (rule, ml,
    heuristic) are stored separately in ``details`` and calibrated
    separately in ``evaluate_outcomes``.
    """
    existing = session.scalar(
        select(models.RiskOutcome).where(
            models.RiskOutcome.score_id == score_id,
        )
    )
    if existing:
        return
    session.add(
        models.RiskOutcome(
            asset_id=asset_id,
            risk_band=risk_band,
            score_id=score_id,
            scored_at=ensure_utc(decision_ts),
            label_version=LABEL_VERSION,
            lifecycle_phase_at_score=(
                _lifecycle_phase_as_of(session, asset_id, decision_ts) or "unknown"
            ),
            details={
                "ml_risk_band": ml_risk_band,
                "ml_prediction": ml_risk_band is not None,
                "heuristic_band": heuristic_band,
                "heuristic_prediction": heuristic_band is not None,
            },
        )
    )
    session.flush()


def evaluate_outcomes(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    window_hours: float | None = None,
) -> RiskOutcomeReport:
    """Evaluate outcomes for all scored tokens within the observation window.

    For each RiskOutcome row whose scored_at + window is in the past,
    check what happened: did the token collapse? rugged? survive?
    Update the outcome row and compute band-level precision metrics.

    Labels are bounded to each prediction's own observation window
    ``[scored_at, scored_at + window]``: only lifecycle state at or before
    window end may label the row.  When no lifecycle phase inside that window
    establishes collapse or survival, the row is left unresolved
    (``evaluated_at`` stays NULL) so it is never counted as a non-survival and
    a later pass can retry it if backfilled data resolves the window.
    """
    settings = get_settings()
    decision_ts = ensure_utc(decision_ts or utc_now())
    window = timedelta(hours=window_hours or settings.risk_outcome_window_hours)

    outcomes = session.scalars(
        select(models.RiskOutcome)
        .where(models.RiskOutcome.evaluated_at.is_(None))
        .order_by(models.RiskOutcome.scored_at.asc())
    ).all()

    evaluated = 0
    unresolved = 0
    for outcome in outcomes:
        score_time = ensure_utc(outcome.scored_at)
        window_end = score_time + window
        if window_end > decision_ts:
            continue
        # Only lifecycle state inside [score_time, window_end] may label this
        # prediction.
        phase = _lifecycle_phase_as_of(session, outcome.asset_id, window_end)
        if phase not in _COLLAPSE_PHASES and phase not in _SURVIVAL_PHASES:
            # Unresolved: no lifecycle state in the window, or a phase that is
            # neither collapse nor survival.  Do NOT stamp evaluated_at — an
            # outcome that cannot be established must not become a failure.
            # Count it so a growing withheld-label backlog is visible.
            outcome.lifecycle_phase_at_eval = phase or "unknown"
            unresolved += 1
            continue
        outcome.lifecycle_phase_at_eval = phase
        outcome.price_change_pct = _price_change_since(
            session, outcome.asset_id, score_time, until=window_end
        )
        outcome.evaluated_at = decision_ts
        outcome.label_version = LABEL_VERSION
        outcome.collapsed = phase in _COLLAPSE_PHASES
        outcome.rugged = phase == "rugged"
        outcome.survived = phase in _SURVIVAL_PHASES
        evaluated += 1

    if evaluated > 0:
        session.flush()

    # Compute band-level metrics
    report = RiskOutcomeReport(
        window_hours=window.total_seconds() / 3600,
        observation_ts=decision_ts,
        unresolved=unresolved,
    )

    # Publish the backlog to engine health and flag growth, so withheld labels
    # cannot accumulate silently between passes.
    previous_unresolved = session.scalar(
        select(models.SystemHealth.error_count)
        .where(models.SystemHealth.component == UNRESOLVED_OUTCOME_COMPONENT)
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    grew = previous_unresolved is not None and unresolved > previous_unresolved
    report.unresolved_grew = grew
    # State is age-based: an unresolved row older than three observation
    # windows means the label pipeline is not resolving, which is red. A
    # younger backlog is yellow; zero backlog is ok. Growth alone stays yellow.
    oldest = session.scalar(
        select(func.min(models.RiskOutcome.scored_at)).where(
            models.RiskOutcome.evaluated_at.is_(None),
            models.RiskOutcome.label_version == LABEL_VERSION,
        )
    )
    age_limit = timedelta(hours=3 * settings.risk_outcome_window_hours)
    if not unresolved:
        state = "ok"
    elif oldest is not None and decision_ts - ensure_utc(oldest) > age_limit:
        state = "red"
    else:
        state = "yellow"
    record_health(
        session,
        component=UNRESOLVED_OUTCOME_COMPONENT,
        state=state,
        error_count=unresolved,
        message=(
            f"unresolved={unresolved} "
            f"previous={previous_unresolved if previous_unresolved is not None else 'none'}"
            f"{' GREW' if grew else ''}"
        ),
    )

    for band in RiskBand:
        band_outcomes = session.scalars(
            select(models.RiskOutcome).where(
                models.RiskOutcome.risk_band == band.value,
                models.RiskOutcome.evaluated_at.is_not(None),
            )
        ).all()
        if not band_outcomes:
            continue
        total = len(band_outcomes)
        collapsed = sum(1 for o in band_outcomes if o.collapsed)
        rugged = sum(1 for o in band_outcomes if o.rugged)
        survived = sum(1 for o in band_outcomes if o.survived)
        # NB: rugged rows are a subset of collapsed (see outcome.collapsed),
        # so unknown must NOT subtract rugged again or it goes negative.
        unknown = total - collapsed - survived
        precision = collapsed / total if total > 0 else 0.0
        report.bands[band.value] = BandOutcome(
            band=band,
            total_flagged=total,
            collapsed=collapsed,
            rugged=rugged,
            survived=survived,
            unknown=unknown,
            precision=precision,
        )
        report.total_flagged += total
        report.total_collapsed += collapsed

    # Overall precision: what fraction of all flagged tokens collapsed
    if report.total_flagged > 0:
        report.overall_precision = report.total_collapsed / report.total_flagged

    # Compute recall for each band (fraction of all collapses caught)
    total_collapsed_all = sum(b.collapsed for b in report.bands.values())
    if total_collapsed_all > 0:
        for band_outcome in report.bands.values():
            band_outcome.recall = band_outcome.collapsed / total_collapsed_all

    # ── ML-specific band outcomes ────────────────────────────────────────
    # Group evaluated outcomes by the band the ML scorer predicted (stored in
    # details at scoring time).  Only rows where the ML scorer actually made
    # an independent prediction (ml_prediction=True) count, so the ML
    # thresholds learn from the ML signal, not rule-band duplicates.
    ml_outcomes = session.scalars(
        select(models.RiskOutcome).where(
            models.RiskOutcome.evaluated_at.is_not(None),
        )
    ).all()
    ml_by_band: dict[str, list[models.RiskOutcome]] = {}
    for outcome in ml_outcomes:
        details = outcome.details or {}
        if not details.get("ml_prediction"):
            continue
        ml_band = details.get("ml_risk_band")
        if not ml_band:
            continue
        ml_by_band.setdefault(ml_band, []).append(outcome)

    for band_name, band_outcomes in ml_by_band.items():
        total = len(band_outcomes)
        collapsed = sum(1 for o in band_outcomes if o.collapsed)
        rugged = sum(1 for o in band_outcomes if o.rugged)
        survived = sum(1 for o in band_outcomes if o.survived)
        # NB: rugged rows are a subset of collapsed (see outcome.collapsed),
        # so unknown must NOT subtract rugged again or it goes negative.
        unknown = total - collapsed - survived
        report.ml_bands[band_name] = BandOutcome(
            band=RiskBand(band_name),
            total_flagged=total,
            collapsed=collapsed,
            rugged=rugged,
            survived=survived,
            unknown=unknown,
            precision=collapsed / total if total > 0 else 0.0,
        )
        report.ml_total_flagged += total
        report.ml_total_collapsed += collapsed

    log.info(
        "risk_outcome_evaluation",
        evaluated=evaluated,
        unresolved=report.unresolved,
        unresolved_grew=report.unresolved_grew,
        total_flagged=report.total_flagged,
        total_collapsed=report.total_collapsed,
        overall_precision=round(report.overall_precision, 4),
        ml_total_flagged=report.ml_total_flagged,
    )

    # Feed per-token evaluated outcomes into ensemble so adaptive weights
    # learn from ACTUAL individual token results, not band-level aggregates.
    try:
        from scoring.ensemble import ensemble_engine

        # Per-token feedback: iterate individual outcomes, not band aggregates.
        # Only feed outcomes that haven't been sent to the ensemble yet
        # (ensemble_fed_at is NULL = not yet fed).
        recent_outcomes = session.scalars(
            select(models.RiskOutcome).where(
                models.RiskOutcome.evaluated_at.is_not(None),
                models.RiskOutcome.evaluated_at >= decision_ts - window,
                models.RiskOutcome.ensemble_fed_at.is_(None),
            )
        ).all()
        fed_count = 0
        max_feed = 100  # cap per evaluation pass to avoid ensemble overload
        settings = get_settings()
        for row in recent_outcomes:
            if fed_count >= max_feed:
                break
            # Determine actual outcome from this individual token's result
            if row.collapsed or row.rugged:
                actual = "negative"
            elif row.survived:
                actual = "positive"
            else:
                # Unknown lifecycle — use price change as fallback signal
                if (
                    row.price_change_pct is not None
                    and row.price_change_pct < settings.risk_outcome_price_drop_threshold
                ):
                    actual = "negative"
                elif (
                    row.price_change_pct is not None
                    and row.price_change_pct > settings.risk_outcome_price_gain_threshold
                ):
                    actual = "positive"
                else:
                    continue  # skip truly unknown outcomes

            # Feed the outcome to ALL scorers (rule, ml, heuristic) in one
            # batch call — each scorer's prediction was stored separately in
            # RiskOutcome.details at scoring time, so each ensemble layer
            # calibrates independently on the same observed outcome.
            # Gate each scorer on its own *_prediction flag so the rule band
            # fallback can never be attributed to a layer that made no
            # independent prediction (that would train ML/heuristic on the
            # rule scorer's signal).
            details = row.details or {}
            ml_band = details.get("ml_risk_band") if details.get("ml_prediction") else None
            heuristic_band = (
                details.get("heuristic_band") if details.get("heuristic_prediction") else None
            )
            # Confidence-weight the feedback: the ensemble counts a
            # high-confidence prediction's outcome more than a low-confidence
            # guess, so reliable scorers' weights adapt faster.
            score_row = session.get(models.Score, row.score_id)
            confidence = float(score_row.confidence) if score_row is not None else None
            entries = [
                {
                    "scorer_name": "rule",
                    "predicted_band": row.risk_band,
                    "actual_outcome": actual,
                    "confidence": confidence,
                }
            ]
            if ml_band:
                entries.append(
                    {
                        "scorer_name": "ml",
                        "predicted_band": ml_band,
                        "actual_outcome": actual,
                        "confidence": confidence,
                    }
                )
            if heuristic_band:
                entries.append(
                    {
                        "scorer_name": "heuristic",
                        "predicted_band": heuristic_band,
                        "actual_outcome": actual,
                        "confidence": confidence,
                    }
                )
            ensemble_engine.record_outcomes(entries)
            row.ensemble_fed_at = decision_ts
            fed_count += 1
    except Exception:  # noqa: BLE001
        pass  # ensemble feedback must never break outcome evaluation

    return report


def record_prediction(
    session: Session,
    *,
    asset_id: int,
    scored_at: datetime,
    evidence_ts: datetime,
    risk_band: str,
    risk_score: float,
    confidence: float,
    confidence_source: str,
    missing_features: dict,
    code_sha: str | None = None,
    config_sha256: str | None = None,
    calibration_snapshot_id: str | None = None,
) -> models.PredictionRecord:
    """Record a prediction for a token at scoring time.

    The prediction is stored so it can be compared against the actual outcome
    when the observation window resolves. The calibrator uses these records
    to adjust ensemble weights dynamically.
    """
    record = models.PredictionRecord(
        asset_id=asset_id,
        scored_at=scored_at,
        evidence_ts=evidence_ts,
        risk_band=risk_band,
        risk_score=risk_score,
        confidence=confidence,
        confidence_source=confidence_source,
        missing_features=missing_features,
        code_sha=code_sha,
        config_sha256=config_sha256,
        calibration_snapshot_id=calibration_snapshot_id,
    )
    session.add(record)
    session.flush()
    return record
