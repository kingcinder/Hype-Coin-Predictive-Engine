from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from scoring.cross_source_fusion import _count_distinct_sources, fuse_signals
from storage import models
from storage.repository import get_or_create_source, insert_market_snapshot_once
from tests.conftest import seed_market_asset


def _now() -> datetime:
    """Real current time so seeded events fall inside fuse_signals' 6h window."""
    return datetime.now(UTC)


def _make_source(session, name: str) -> models.Source:
    return get_or_create_source(
        session,
        name=name,
        source_type="social",
        tier="community",
        base_url=f"https://{name}.example.com",
    )


def test_count_distinct_sources_single_union_merges_all_event_types(session) -> None:
    """The UNION ALL query sees activity across all four event branches."""
    asset = seed_market_asset(session)
    now = _now()
    cutoff = now - timedelta(hours=6)

    dexscreener = _make_source(session, "dexscreener2")
    twitter = _make_source(session, "twitter2")
    farcaster = _make_source(session, "farcaster2")
    mempool = _make_source(session, "mempool2")

    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    assert pair is not None
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=dexscreener.id,
        ts=now,
        observed_at=now,
        price_usd=1.5,
        volume_usd=50_000,
        buys=30,
        sells=3,
        trades=33,
    )
    session.add(
        models.SocialMention(
            asset_id=asset.id,
            topic="hype",
            source_id=twitter.id,
            ts=now,
            observed_at=now,
            author_hash="abc",
            metrics_json={},
            raw_ref="https://twitter.com/x",
        )
    )
    session.add(
        models.IgnitionEvent(
            asset_id=asset.id,
            source_id=farcaster.id,
            event_type="early_volume_spike",
            ts=now,
            observed_at=now,
            confidence=0.8,
            details={},
        )
    )
    session.add(
        models.LiquidityRemovalEvent(
            asset_id=asset.id,
            pool_id=pair.pool_id,
            source_id=mempool.id,
            chain_slug="solana",
            event_kind="lp_burn",
            tx_hash="0xabc123",
            log_index=0,
            block_number=1000,
            ts=now,
            observed_at=now,
            confidence=0.9,
            details={},
        )
    )
    session.commit()

    count, names = _count_distinct_sources(session, asset.id, cutoff)
    assert count == 4
    assert names == sorted(["dexscreener2", "twitter2", "farcaster2", "mempool2"])

    # fuse_signals (uses utc_now window) sees the corroboration too
    result = fuse_signals(session, asset.id)
    assert result.source_count == 4
    assert result.confidence_boost > 0.0


def test_count_distinct_sources_excludes_stale_activity(session) -> None:
    """Events older than the cutoff do not count toward source corroboration."""
    asset = seed_market_asset(session)
    now = _now()
    cutoff = now - timedelta(hours=6)

    twitter = _make_source(session, "twitter3")
    session.add(
        models.SocialMention(
            asset_id=asset.id,
            topic="hype",
            source_id=twitter.id,
            ts=now - timedelta(days=2),
            observed_at=now - timedelta(days=2),
            author_hash="def",
            metrics_json={},
            raw_ref="https://twitter.com/stale",
        )
    )
    session.commit()

    count, names = _count_distinct_sources(session, asset.id, cutoff)
    assert count == 0
    assert names == []

    result = fuse_signals(session, asset.id)
    assert result.source_count == 0
    assert result.confidence_boost == 0.0


def test_count_distinct_sources_deduplicates_same_source_across_branches(session) -> None:
    """The same source reporting via multiple channels counts once."""
    asset = seed_market_asset(session)
    now = _now()
    cutoff = now - timedelta(hours=6)

    twitter = _make_source(session, "twitter4")
    session.add(
        models.SocialMention(
            asset_id=asset.id,
            topic="hype",
            source_id=twitter.id,
            ts=now,
            observed_at=now,
            author_hash="ghi",
            metrics_json={},
            raw_ref="https://twitter.com/a",
        )
    )
    session.add(
        models.IgnitionEvent(
            asset_id=asset.id,
            source_id=twitter.id,
            event_type="early_volume_spike",
            ts=now,
            observed_at=now,
            confidence=0.7,
            details={},
        )
    )
    session.commit()

    count, names = _count_distinct_sources(session, asset.id, cutoff)
    assert count == 1
    assert names == ["twitter4"]
