"""Signal scoring engine — sieves actionable information from noise.

Each incoming data point (market snapshot, social mention, news item, etc.)
is scored for "signal strength" based on four dimensions:

1. **Novelty**: Is this new information we haven't seen before?
2. **Cross-source corroboration**: Do multiple sources agree?
3. **Temporal relevance**: How recent is this relative to the decision window?
4. **Magnitude**: How significant is the change compared to historical baselines?

High-signal data gets prioritized for feature extraction and scoring.
Low-signal data gets archived but not actively processed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models

log = get_logger(__name__)


@dataclass(frozen=True)
class SignalScore:
    """Result of scoring a single data point for signal strength."""

    source_table: str
    record_id: int
    signal_score: float  # 0.0 (pure noise) to 1.0 (critical signal)
    novelty_score: float
    corroboration_score: float
    temporal_score: float
    magnitude_score: float
    reasons: list[str] = field(default_factory=list)
    actionable: bool = False


@dataclass(frozen=True)
class SignalBatchResult:
    """Aggregated result of scoring a batch of data points."""

    total_scored: int
    actionable_count: int
    noise_count: int
    avg_signal: float
    top_signals: list[SignalScore]
    timestamp: datetime


def _novelty_score(
    session: Session,
    source_table: str,
    record_id: int,
    source_id: int,
    observed_at: datetime,
) -> tuple[float, list[str]]:
    """Score how novel this data point is relative to recent history.

    High novelty = we haven't seen similar data recently.
    Low novelty = duplicate or near-duplicate of recent data.
    """
    reasons: list[str] = []
    window = timedelta(hours=24)

    if source_table == "market_snapshots":
        # Check if we have recent snapshots for the same pair
        row = session.execute(
            select(
                func.count(models.MarketSnapshot.id),
                func.max(models.MarketSnapshot.price_usd),
            ).where(
                models.MarketSnapshot.pair_id == record_id,
                models.MarketSnapshot.observed_at >= observed_at - window,
                models.MarketSnapshot.observed_at < observed_at,
            )
        ).one()
        count = row[0] or 0
        if count == 0:
            reasons.append("first snapshot in 24h window")
            return 1.0, reasons
        # More existing snapshots = less novel
        score = max(0.0, 1.0 - (count / 24.0))
        reasons.append(f"{count} prior snapshots in 24h")
        return score, reasons

    elif source_table == "social_mentions":
        # Check for recent mentions of the same topic
        count = (
            session.scalar(
                select(func.count())
                .select_from(models.SocialMention)
                .where(
                    models.SocialMention.source_id == source_id,
                    models.SocialMention.observed_at >= observed_at - window,
                    models.SocialMention.observed_at < observed_at,
                )
            )
            or 0
        )
        score = max(0.0, 1.0 - (count / 100.0))
        reasons.append(f"{count} recent mentions from same source")
        return score, reasons

    elif source_table == "ignition_events":
        # Ignition events are always novel
        reasons.append("ignition event — always actionable")
        return 1.0, reasons

    # Default: moderate novelty
    reasons.append("default novelty for unknown table")
    return 0.5, reasons


def _corroboration_score(
    session: Session,
    asset_id: int | None,
    source_table: str,
    observed_at: datetime,
) -> tuple[float, list[str]]:
    """Score cross-source corroboration.

    High corroboration = multiple independent sources agree.
    Low corroboration = single-source unconfirmed report.
    """
    reasons: list[str] = []
    if asset_id is None:
        return 0.0, ["no asset_id for corroboration"]

    window = timedelta(hours=6)
    source_count = (
        session.scalar(
            select(func.count(func.distinct(models.MarketSnapshot.source_id))).where(
                models.MarketSnapshot.pair_id.in_(
                    select(models.Pair.id).where(models.Pair.base_asset_id == asset_id)
                ),
                models.MarketSnapshot.observed_at >= observed_at - window,
                models.MarketSnapshot.observed_at <= observed_at,
            )
        )
        or 0
    )

    if source_count >= 3:
        reasons.append(f"{source_count} sources corroborate")
        return 1.0, reasons
    elif source_count >= 2:
        reasons.append(f"{source_count} sources corroborate")
        return 0.7, reasons
    elif source_count == 1:
        reasons.append("single source")
        return 0.3, reasons
    else:
        reasons.append("no corroborating sources")
        return 0.0, reasons


def _temporal_score(observed_at: datetime, decision_ts: datetime) -> tuple[float, list[str]]:
    """Score temporal relevance — how fresh is this data?

    Very recent = high score. Stale = low score.
    """
    reasons: list[str] = []
    age_hours = max(0.0, (decision_ts - observed_at).total_seconds() / 3600.0)

    if age_hours <= 1:
        reasons.append(f"data is {age_hours:.1f}h old — very fresh")
        return 1.0, reasons
    elif age_hours <= 6:
        reasons.append(f"data is {age_hours:.1f}h old — fresh")
        return 0.8, reasons
    elif age_hours <= 24:
        reasons.append(f"data is {age_hours:.1f}h old — moderate")
        return 0.5, reasons
    else:
        reasons.append(f"data is {age_hours:.1f}h old — stale")
        return max(0.0, 1.0 - (age_hours / 168.0)), reasons


def _magnitude_score(
    session: Session,
    asset_id: int | None,
    current_price: float | None,
    observed_at: datetime,
) -> tuple[float, list[str]]:
    """Score magnitude of change relative to historical baseline.

    Large price/volume moves = high signal. Small moves = noise.
    """
    reasons: list[str] = []
    if asset_id is None or current_price is None or current_price <= 0:
        reasons.append("insufficient price data for magnitude")
        return 0.5, reasons

    # Get the price from 24h ago
    prior = session.scalar(
        select(models.MarketSnapshot.price_usd)
        .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
        .where(
            models.Pair.base_asset_id == asset_id,
            models.MarketSnapshot.ts <= observed_at - timedelta(hours=24),
            models.MarketSnapshot.price_usd.is_not(None),
            models.MarketSnapshot.price_usd > 0,
        )
        .order_by(models.MarketSnapshot.ts.desc())
        .limit(1)
    )

    if prior is None:
        reasons.append("no historical price for magnitude comparison")
        return 0.5, reasons

    change_pct = abs(current_price / prior - 1.0) * 100.0

    if change_pct >= 50:
        reasons.append(f"price moved {change_pct:.1f}% in 24h — extreme")
        return 1.0, reasons
    elif change_pct >= 20:
        reasons.append(f"price moved {change_pct:.1f}% in 24h — significant")
        return 0.8, reasons
    elif change_pct >= 5:
        reasons.append(f"price moved {change_pct:.1f}% in 24h — moderate")
        return 0.5, reasons
    else:
        reasons.append(f"price moved {change_pct:.1f}% in 24h — minor")
        return 0.2, reasons


def score_market_snapshot(
    session: Session,
    snapshot: models.MarketSnapshot,
    decision_ts: datetime | None = None,
) -> SignalScore:
    """Score a single market snapshot for signal strength."""
    decision_ts = ensure_utc(decision_ts or utc_now())

    # Get the pair and asset
    pair = session.get(models.Pair, snapshot.pair_id)
    asset_id = pair.base_asset_id if pair else None

    novelty, n_reasons = _novelty_score(
        session, "market_snapshots", snapshot.pair_id, snapshot.source_id, snapshot.observed_at
    )
    corroboration, c_reasons = _corroboration_score(
        session, asset_id, "market_snapshots", snapshot.observed_at
    )
    temporal, t_reasons = _temporal_score(snapshot.observed_at, decision_ts)
    magnitude, m_reasons = _magnitude_score(
        session, asset_id, snapshot.price_usd, snapshot.observed_at
    )

    # Weighted combination
    signal = 0.25 * novelty + 0.25 * corroboration + 0.20 * temporal + 0.30 * magnitude

    return SignalScore(
        source_table="market_snapshots",
        record_id=snapshot.id,
        signal_score=round(signal, 4),
        novelty_score=round(novelty, 4),
        corroboration_score=round(corroboration, 4),
        temporal_score=round(temporal, 4),
        magnitude_score=round(magnitude, 4),
        reasons=n_reasons + c_reasons + t_reasons + m_reasons,
        actionable=signal >= 0.4,
    )


def score_social_mention(
    session: Session,
    mention: models.SocialMention,
    decision_ts: datetime | None = None,
) -> SignalScore:
    """Score a social mention for signal strength."""
    decision_ts = ensure_utc(decision_ts or utc_now())

    novelty, n_reasons = _novelty_score(
        session, "social_mentions", mention.id, mention.source_id, mention.observed_at
    )
    temporal, t_reasons = _temporal_score(mention.observed_at, decision_ts)

    # Social mentions don't have magnitude in the same way
    magnitude = 0.5
    corroboration, c_reasons = _corroboration_score(
        session, mention.asset_id, "social_mentions", mention.observed_at
    )

    signal = 0.30 * novelty + 0.30 * temporal + 0.20 * corroboration + 0.20 * magnitude

    return SignalScore(
        source_table="social_mentions",
        record_id=mention.id,
        signal_score=round(signal, 4),
        novelty_score=round(novelty, 4),
        corroboration_score=round(corroboration, 4),
        temporal_score=round(temporal, 4),
        magnitude_score=round(magnitude, 4),
        reasons=n_reasons + c_reasons + t_reasons,
        actionable=signal >= 0.4,
    )


def score_ignition_event(
    session: Session,
    event: models.IgnitionEvent,
    decision_ts: datetime | None = None,
) -> SignalScore:
    """Score an ignition event — always high signal."""
    decision_ts = ensure_utc(decision_ts or utc_now())
    temporal, t_reasons = _temporal_score(event.observed_at, decision_ts)

    # Ignition events are always actionable
    return SignalScore(
        source_table="ignition_events",
        record_id=event.id,
        signal_score=round(min(1.0, 0.8 * event.confidence + 0.2 * temporal), 4),
        novelty_score=1.0,
        corroboration_score=event.confidence,
        temporal_score=round(temporal, 4),
        magnitude_score=event.confidence,
        reasons=["ignition event — always actionable", *t_reasons],
        actionable=True,
    )


def score_batch(
    session: Session,
    decision_ts: datetime | None = None,
    limit: int = 500,
) -> SignalBatchResult:
    """Score a batch of recent data points for signal strength.

    Returns the aggregated result with top signals and counts.
    """
    decision_ts = ensure_utc(decision_ts or utc_now())
    scores: list[SignalScore] = []

    # Score recent market snapshots
    cutoff = decision_ts - timedelta(hours=24)
    snapshots = session.scalars(
        select(models.MarketSnapshot)
        .where(models.MarketSnapshot.observed_at >= cutoff)
        .order_by(models.MarketSnapshot.observed_at.desc())
        .limit(limit // 2)
    ).all()
    for snap in snapshots:
        scores.append(score_market_snapshot(session, snap, decision_ts))

    # Score recent social mentions
    mentions = session.scalars(
        select(models.SocialMention)
        .where(models.SocialMention.observed_at >= cutoff)
        .order_by(models.SocialMention.observed_at.desc())
        .limit(limit // 4)
    ).all()
    for mention in mentions:
        scores.append(score_social_mention(session, mention, decision_ts))

    # Score recent ignition events
    ignition_cutoff = decision_ts - timedelta(hours=24)
    events = session.scalars(
        select(models.IgnitionEvent)
        .where(models.IgnitionEvent.observed_at >= ignition_cutoff)
        .order_by(models.IgnitionEvent.observed_at.desc())
        .limit(limit // 4)
    ).all()
    for event in events:
        scores.append(score_ignition_event(session, event, decision_ts))

    # Sort by signal strength
    scores.sort(key=lambda s: s.signal_score, reverse=True)

    actionable = [s for s in scores if s.actionable]
    avg_signal = sum(s.signal_score for s in scores) / max(1, len(scores))

    return SignalBatchResult(
        total_scored=len(scores),
        actionable_count=len(actionable),
        noise_count=len(scores) - len(actionable),
        avg_signal=round(avg_signal, 4),
        top_signals=scores[:20],
        timestamp=decision_ts,
    )
