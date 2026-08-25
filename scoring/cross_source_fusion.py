"""Cross-source signal fusion — aggregates multi-crawler signals into a unified confidence boost.

When multiple independent Night Crawlers (Twitter, Farcaster, DEX scanners,
contract analyzers, mempool monitors, etc.) all detect the same asset, the
cross-source corroboration increases confidence that the signal is real.

This module queries the database for recent activity across all source types
for a given asset, computes a fusion score (0.0–1.0), and returns a confidence
boost that the ensemble blend layer adds to its final confidence estimate.

High corroboration (3+ independent sources within 6h) → up to +15 confidence.
Single source → +0 boost (insufficient corroboration).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging import get_logger
from common.time import utc_now
from storage import models

log = get_logger(__name__)

# Boost caps and thresholds
_MAX_CONFIDENCE_BOOST = 15.0
_MIN_SOURCES_FOR_BOOST = 2
_FUSION_WINDOW_HOURS = 6.0


@dataclass(frozen=True)
class FusionResult:
    """Result of cross-source signal fusion for a single asset."""

    asset_id: int
    source_count: int
    sources: list[str]
    fusion_score: float  # 0.0 to 1.0
    confidence_boost: float  # 0.0 to _MAX_CONFIDENCE_BOOST
    signal_agreement: float  # fraction of sources with actionable signals


def _count_distinct_sources(
    session: Session,
    asset_id: int,
    cutoff: datetime,
) -> tuple[int, list[str]]:
    """Count distinct source types that have recent activity for this asset.

    A single UNION ALL query covers market snapshots (via pairs), social
    mentions, ignition events, and liquidity removal events — one round
    trip instead of four separate queries plus a pair-id pre-query.  The
    market branch joins ``Pair`` directly on ``base_asset_id``, so no
    intermediate id list is needed.
    """
    # Per-branch DISTINCT keeps SQL-side dedup so a high-volume source
    # (e.g. thousands of mentions in the window) ships one row per name
    # instead of every duplicate row through the UNION ALL.
    market_branch = (
        select(models.Source.name)
        .distinct()
        .join(
            models.MarketSnapshot,
            models.MarketSnapshot.source_id == models.Source.id,
        )
        .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
        .where(
            models.Pair.base_asset_id == asset_id,
            models.MarketSnapshot.observed_at >= cutoff,
        )
    )
    social_branch = (
        select(models.Source.name)
        .distinct()
        .join(
            models.SocialMention,
            models.SocialMention.source_id == models.Source.id,
        )
        .where(
            models.SocialMention.asset_id == asset_id,
            models.SocialMention.observed_at >= cutoff,
        )
    )
    ignition_branch = (
        select(models.Source.name)
        .distinct()
        .join(
            models.IgnitionEvent,
            models.IgnitionEvent.source_id == models.Source.id,
        )
        .where(
            models.IgnitionEvent.asset_id == asset_id,
            models.IgnitionEvent.observed_at >= cutoff,
        )
    )
    liq_branch = (
        select(models.Source.name)
        .distinct()
        .join(
            models.LiquidityRemovalEvent,
            models.LiquidityRemovalEvent.source_id == models.Source.id,
        )
        .where(
            models.LiquidityRemovalEvent.asset_id == asset_id,
            models.LiquidityRemovalEvent.observed_at >= cutoff,
        )
    )

    combined = market_branch.union_all(social_branch, ignition_branch, liq_branch)
    source_names = set(session.scalars(combined).all())
    return len(source_names), sorted(source_names)


def fuse_signals(
    session: Session,
    asset_id: int,
) -> FusionResult:
    """Compute cross-source signal fusion for a single asset.

    Queries all source types with recent activity, counts distinct sources,
    and computes a fusion score that maps to a confidence boost.
    """
    now = utc_now()
    cutoff = now - timedelta(hours=_FUSION_WINDOW_HOURS)

    source_count, source_names = _count_distinct_sources(session, asset_id, cutoff)

    if source_count < _MIN_SOURCES_FOR_BOOST:
        return FusionResult(
            asset_id=asset_id,
            source_count=source_count,
            sources=source_names,
            fusion_score=0.0,
            confidence_boost=0.0,
            signal_agreement=0.0,
        )

    # Fusion score: logarithmic scale favoring more sources
    # 2 sources → 0.4, 3 → 0.65, 4 → 0.82, 5+ → 0.95+
    fusion_score = min(1.0, math.log1p(source_count) / math.log1p(6))

    # Confidence boost: fusion_score mapped to [0, _MAX_CONFIDENCE_BOOST]
    confidence_boost = round(fusion_score * _MAX_CONFIDENCE_BOOST, 4)

    # Signal agreement: for now, all sources with data agree (simple heuristic)
    signal_agreement = min(1.0, source_count / max(_MIN_SOURCES_FOR_BOOST, source_count))

    return FusionResult(
        asset_id=asset_id,
        source_count=source_count,
        sources=source_names,
        fusion_score=round(fusion_score, 4),
        confidence_boost=confidence_boost,
        signal_agreement=round(signal_agreement, 4),
    )


def persist_fusion(
    session: Session,
    result: FusionResult,
) -> None:
    """Persist a fusion result to the database for historical tracking."""
    session.add(
        models.CrossSourceSignal(
            asset_id=result.asset_id,
            source_count=result.source_count,
            sources=result.sources,
            fusion_score=result.fusion_score,
            confidence_boost=result.confidence_boost,
            signal_agreement=result.signal_agreement,
            observed_at=utc_now(),
        )
    )
