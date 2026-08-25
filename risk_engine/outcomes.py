"""Risk Outcome Tracker — records actual outcomes for flagged tokens.

After each scoring pass, this module looks up what happened to tokens
that were previously flagged at each risk band, computing precision
(recall, hit rate) for each band.  This data feeds the adaptive
calibrator so risk thresholds learn from real market outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import RiskBand
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models

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
    # ML-specific band outcomes, keyed by the band the ML scorer predicted
    # (from RiskOutcome.details["ml_risk_band"]).  These feed the calibrator's
    # ML probability thresholds so the ML scorer calibrates independently of
    # the rule engine.
    ml_bands: dict[str, BandOutcome] = field(default_factory=dict)
    ml_total_flagged: int = 0
    ml_total_collapsed: int = 0


def _latest_lifecycle_phase(session: Session, asset_id: int) -> str | None:
    """Return the most recent lifecycle phase for an asset."""
    event = session.scalar(
        select(models.LifecycleEvent)
        .where(models.LifecycleEvent.asset_id == asset_id)
        .order_by(models.LifecycleEvent.ts.desc())
        .limit(1)
    )
    return event.phase if event else None


def _price_change_since(session: Session, asset_id: int, since: datetime) -> float | None:
    """Return the percentage price change from `since` to the latest snapshot."""
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
            lifecycle_phase_at_score=(_latest_lifecycle_phase(session, asset_id) or "unknown"),
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
    for outcome in outcomes:
        score_time = ensure_utc(outcome.scored_at)
        if score_time + window > decision_ts:
            continue
        # Evaluate: check lifecycle phase and price change
        phase = _latest_lifecycle_phase(session, outcome.asset_id)
        price_change = _price_change_since(session, outcome.asset_id, score_time)
        outcome.lifecycle_phase_at_eval = phase or "unknown"
        outcome.price_change_pct = price_change
        outcome.evaluated_at = decision_ts
        outcome.collapsed = phase in ("collapse", "dead", "rugged")
        outcome.rugged = phase == "rugged"
        outcome.survived = phase in ("survivor", "parabolic", "saturation")
        evaluated += 1

    if evaluated > 0:
        session.flush()

    # Compute band-level metrics
    report = RiskOutcomeReport(
        window_hours=window.total_seconds() / 3600,
        observation_ts=decision_ts,
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
        unknown = total - collapsed - rugged - survived
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
        unknown = total - collapsed - rugged - survived
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
