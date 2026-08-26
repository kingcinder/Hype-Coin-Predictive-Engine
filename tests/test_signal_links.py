"""Tests for crawler item → asset linking (crawlers/signal_links.py) and its
integration with cross-source fusion."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from crawlers.signal_links import link_crawler_items
from scoring.cross_source_fusion import fuse_signals
from storage import models
from storage.repository import get_or_create_source
from tests.conftest import seed_market_asset

# Real current time: fuse_signals only counts activity within its 6h window,
# so seeded events must use now (same convention as test_cross_source_fusion).
NOW = datetime.now(UTC)


def _crawler_source(session, name: str = "pump_portal") -> models.Source:
    return get_or_create_source(
        session,
        name=f"nightcrawler:{name}",
        source_type="nightcrawler",
        tier="enriched",
        base_url=None,
    )


def _mention_count(session, asset_id: int, source_id: int) -> int:
    return session.scalar(
        select(func.count(models.SocialMention.id)).where(
            models.SocialMention.asset_id == asset_id,
            models.SocialMention.source_id == source_id,
        )
    )


def test_links_by_explicit_mint_address(session) -> None:
    asset = seed_market_asset(session)
    source = _crawler_source(session)
    items = [
        {
            "title": "New pump.fun launch",
            "text": "fresh token on the curve",
            "url": "https://pump.fun/coin/TOKEN111111111111111111111111111111111111",
            "metrics": {"mint": asset.address, "symbol": asset.symbol},
        }
    ]
    linked = link_crawler_items(session, source=source, items=items, observed_at=NOW)
    session.commit()
    assert linked == 1
    assert _mention_count(session, asset.id, source.id) == 1


def test_links_by_symbol_metrics(session) -> None:
    asset = seed_market_asset(session)
    source = _crawler_source(session, name="coinpaprika")
    items = [
        {
            "title": "HYPE up 42%",
            "text": "top gainer today",
            "url": "https://coinpaprika.com/coin/hype-hype/",
            "metrics": {"symbol": asset.symbol, "gainer": True},
        }
    ]
    linked = link_crawler_items(session, source=source, items=items, observed_at=NOW)
    session.commit()
    assert linked == 1
    assert _mention_count(session, asset.id, source.id) == 1


def test_links_by_token_mention_in_text(session) -> None:
    asset = seed_market_asset(session)
    source = _crawler_source(session, name="google_trends")
    # No explicit address/symbol in metrics — the $HYPE mention in text links it.
    items = [
        {
            "title": "Trending: hype",
            "text": "search volume climbing for $HYPE everywhere",
            "url": "https://www.google.com/search?q=hype",
            "metrics": {"query": "hype", "narrative_momentum": True},
        }
    ]
    linked = link_crawler_items(session, source=source, items=items, observed_at=NOW)
    session.commit()
    assert linked == 1
    assert _mention_count(session, asset.id, source.id) == 1


def test_skips_items_with_no_resolvable_asset(session) -> None:
    seed_market_asset(session)  # exists, but item references nothing it knows
    source = _crawler_source(session, name="gas_tracker")
    items = [
        {
            "title": "Ethereum gas spike",
            "text": "network gas is 125 gwei",
            "url": "https://etherscan.io/gastracker",
            "metrics": {"chain": "ethereum", "regime": "spike"},
        }
    ]
    linked = link_crawler_items(session, source=source, items=items, observed_at=NOW)
    session.commit()
    assert linked == 0
    assert _mention_count(session, 1, source.id) == 0


def test_dedupes_same_item_on_recrawl(session) -> None:
    asset = seed_market_asset(session)
    source = _crawler_source(session)
    items = [
        {
            "title": "New pump.fun launch",
            "text": "fresh token on the curve",
            "url": "https://pump.fun/coin/TOKEN111111111111111111111111111111111111",
            "metrics": {"mint": asset.address, "symbol": asset.symbol},
        }
    ]
    link_crawler_items(session, source=source, items=items, observed_at=NOW)
    session.commit()
    link_crawler_items(session, source=source, items=items, observed_at=NOW)
    session.commit()
    assert _mention_count(session, asset.id, source.id) == 1


def test_linked_crawler_sources_reach_fusion(session) -> None:
    """The whole point: a linked crawler + another source → fusion boost."""
    asset = seed_market_asset(session)
    pump = _crawler_source(session, name="pump_portal")
    link_crawler_items(
        session,
        source=pump,
        items=[
            {
                "title": "New pump.fun launch",
                "text": "fresh token on the curve",
                "url": "https://pump.fun/coin/TOKEN111111111111111111111111111111111111",
                "metrics": {"mint": asset.address, "symbol": asset.symbol},
            }
        ],
        observed_at=NOW,
    )
    # A second, independent source: a social mention on its own source.
    twitter = get_or_create_source(
        session,
        name="twitter_fusion",
        source_type="social",
        tier="community",
        base_url="https://twitter.example.com",
    )
    session.add(
        models.SocialMention(
            asset_id=asset.id,
            topic="hype",
            source_id=twitter.id,
            ts=NOW,
            observed_at=NOW,
            author_hash="abc",
            metrics_json={},
            raw_ref="https://twitter.com/fusion",
        )
    )
    session.commit()

    result = fuse_signals(session, asset.id)
    # pump_portal (prefix stripped) + twitter_fusion = 2 distinct sources.
    assert result.source_count == 2
    assert result.confidence_boost > 0.0
    assert "pump_portal" in result.sources
    assert "nightcrawler:pump_portal" not in result.sources


def test_single_crawler_source_no_boost_yet(session) -> None:
    """One crawler alone is corroboration of nothing — fusion stays at zero."""
    asset = seed_market_asset(session)
    pump = _crawler_source(session, name="pump_portal")
    link_crawler_items(
        session,
        source=pump,
        items=[
            {
                "title": "New pump.fun launch",
                "text": "fresh token on the curve",
                "url": "https://pump.fun/coin/TOKEN111111111111111111111111111111111111",
                "metrics": {"mint": asset.address, "symbol": asset.symbol},
            }
        ],
        observed_at=NOW,
    )
    session.commit()
    result = fuse_signals(session, asset.id)
    assert result.source_count == 1
    assert result.confidence_boost == 0.0
