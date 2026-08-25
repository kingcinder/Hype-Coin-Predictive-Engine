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
) -> None:
    """Record a single risk outcome observation.

    Called after scoring to snapshot the token's state at scoring time
    so it can be evaluated later when the forward window elapses.
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

    log.info(
        "risk_outcome_evaluation",
        evaluated=evaluated,
        total_flagged=report.total_flagged,
        total_collapsed=report.total_collapsed,
        overall_precision=round(report.overall_precision, 4),
    )

    # Feed evaluated outcomes into ensemble so adaptive weights learn
    # from ACTUAL delayed outcomes, not synthetic predictions.
    try:
        from scoring.ensemble import ensemble_engine

        for band_name, band_outcome in report.bands.items():
            if band_outcome.total_flagged == 0:
                continue
            # Bands with high precision (many collapses) confirm "negative"
            # prediction was correct; low precision means rule was wrong.
            actual = "negative" if band_outcome.precision >= 0.5 else "positive"
            for _ in range(min(band_outcome.total_flagged, 20)):
                ensemble_engine.record_outcome(
                    scorer_name="rule",
                    predicted_band=band_name,
                    actual_outcome=actual,
                )
    except Exception:  # noqa: BLE001
        pass  # ensemble feedback must never break outcome evaluation

    return report
