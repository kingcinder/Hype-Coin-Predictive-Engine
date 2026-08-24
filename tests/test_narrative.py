from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import respx
from httpx import Response
from sqlalchemy import func, select

from narrative.crawlers import (
    RedditCrawler,
    TelegramCrawler,
    YouTubeCrawler,
    normalize_telegram_message,
)
from narrative.embed import MinhashEmbedder
from narrative.engine import NarrativeEngine, get_narrative_endpoint_pool
from storage import models
from tests.conftest import seed_market_asset

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_minhash_similarity_separates_campaigns() -> None:
    embedder = MinhashEmbedder(sig_size=64)
    campaign_a = "HYPE token is launching soon with a big presale on Solana x100 gem"
    campaign_a_retweet = "HYPE token is launching soon with a big presale on Solana x100 gem!!!"
    unrelated = "bitcoin price analysis today fed decision"
    assert embedder.similarity(
        embedder.embed(campaign_a), embedder.embed(campaign_a_retweet)
    ) > 0.5
    assert embedder.similarity(
        embedder.embed(campaign_a), embedder.embed(unrelated)
    ) < 0.3


@respx.mock
def test_reddit_crawler_parses_public_json() -> None:
    respx.get("https://www.reddit.com/r/CryptoMoonShots/new.json").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "children": [
                        {
                            "data": {
                                "title": "HYPE token presale",
                                "permalink": "/r/CryptoMoonShots/comments/abc",
                                "selftext": "gem",
                                "author": "shillbot",
                                "ups": 10,
                                "num_comments": 2,
                            }
                        }
                    ]
                }
            },
        )
    )
    crawler = RedditCrawler(["CryptoMoonShots"])
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 1
    assert items[0]["title"] == "HYPE token presale"
    assert items[0]["source_domain"] == "reddit.com"


@respx.mock
def test_youtube_crawler_parses_rss() -> None:
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><title>HYPE token moonshot</title>"
        '<link href="https://youtu.be/abc"/>'
        "<published>2026-05-01T10:00:00Z</published>"
        "<author><name>KOL</name></author>"
        "</entry></feed>"
    )
    respx.get("https://www.youtube.com/feeds/videos.xml?channel_id=UC123").mock(
        return_value=Response(200, text=rss)
    )
    crawler = YouTubeCrawler(["UC123"])
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 1
    assert items[0]["title"] == "HYPE token moonshot"
    assert items[0]["author"] == "KOL"


def test_telegram_normalization_preserves_public_message() -> None:
    message = SimpleNamespace(
        message="HYPE token presale is live on Solana!",
        id=12345,
        date=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
        sender_id=987654321,
        views=1500,
        forwards=40,
    )
    normalized = normalize_telegram_message(message, channel_handle="@SolanaShills")
    assert normalized is not None
    assert normalized["title"] == "HYPE token presale is live on Solana!"
    assert normalized["url"] == "https://t.me/SolanaShills/12345"
    assert normalized["published"] == datetime(2026, 5, 1, 9, 30, tzinfo=UTC)
    assert normalized["author"] == "987654321"
    assert normalized["source_domain"] == "t.me"
    assert normalized["metrics"]["views"] == 1500
    assert normalize_telegram_message(SimpleNamespace(message=""), channel_handle="@x") is None


def test_telegram_public_channel_gate() -> None:
    assert TelegramCrawler._is_public_channel(
        SimpleNamespace(username="solana_shills", broadcast=True)
    )
    assert not TelegramCrawler._is_public_channel(
        SimpleNamespace(username=None, broadcast=True)
    )
    assert not TelegramCrawler._is_public_channel(
        SimpleNamespace(username="private_group", broadcast=False)
    )


@respx.mock
def test_narrative_pool_probes_all_endpoints_and_skips_dead_one(session) -> None:
    from common.config import get_settings

    settings = get_settings()
    previous = {
        "reddit_endpoint_pool_csv": settings.reddit_endpoint_pool_csv,
        "reddit_subreddits_csv": settings.reddit_subreddits_csv,
        "narrative_background_probe_enabled": settings.narrative_background_probe_enabled,
        "narrative_endpoint_failure_threshold": settings.narrative_endpoint_failure_threshold,
        "youtube_channels_csv": settings.youtube_channels_csv,
        "github_search_queries_csv": settings.github_search_queries_csv,
        "hf_trending_enabled": settings.hf_trending_enabled,
        "rss_feed_urls_csv": settings.rss_feed_urls_csv,
    }
    try:
        settings.reddit_endpoint_pool_csv = "https://dead.reddit.test,https://live.reddit.test"
        settings.reddit_subreddits_csv = "CryptoMoonShots"
        settings.narrative_background_probe_enabled = False
        settings.narrative_endpoint_failure_threshold = 1
        settings.youtube_channels_csv = ""
        settings.github_search_queries_csv = ""
        settings.hf_trending_enabled = False
        settings.rss_feed_urls_csv = ""
        respx.get("https://dead.reddit.test/r/CryptoMoonShots/new.json").mock(
            return_value=Response(503)
        )
        payload = {
            "data": {
                "children": [{"data": {"title": "HYPE from live Reddit"}}]
            }
        }
        respx.get("https://live.reddit.test/r/CryptoMoonShots/new.json").mock(
            return_value=Response(200, json=payload)
        )
        engine = NarrativeEngine()
        counts = engine.crawl(session, decision_ts=NOW)
        assert counts["reddit"] == 1
        pool = get_narrative_endpoint_pool(
            "reddit", tuple(settings.reddit_endpoint_pool_csv.split(",")), 1
        )
        states = {state.url: state for state in pool.snapshot()}
        assert states["https://dead.reddit.test"].down is True
        assert states["https://dead.reddit.test"].last_probe_ok is False
        assert states["https://live.reddit.test"].last_probe_ok is True
    finally:
        for name, value in previous.items():
            setattr(settings, name, value)


def test_telegram_crawler_skipped_without_credentials(session) -> None:
    seed_market_asset(session)
    from common.config import get_settings

    settings = get_settings()
    settings.telegram_enabled = False
    settings.telegram_api_id = None
    settings.telegram_api_hash = None
    engine = NarrativeEngine()
    assert engine._telegram_crawler() is None

    settings.telegram_enabled = True
    assert engine._telegram_crawler() is None  # still no credentials

    settings.telegram_api_id = 12345
    settings.telegram_api_hash = "deadbeef"
    settings.telegram_channel_handles_csv = "@SolanaShills"
    crawler = engine._telegram_crawler()
    assert crawler is not None
    assert crawler.channel_handles == ["@SolanaShills"]


def test_telegram_crawler_constructs_from_settings(session) -> None:
    seed_market_asset(session)
    from common.config import get_settings

    settings = get_settings()
    settings.telegram_enabled = True
    settings.telegram_api_id = 999
    settings.telegram_api_hash = "abc123"
    settings.telegram_channel_handles_csv = "@ShillA,@ShillB"
    engine = NarrativeEngine()
    crawler = engine._telegram_crawler()
    assert crawler is not None
    assert crawler.channel_handles == ["@ShillA", "@ShillB"]
    assert crawler.message_limit == settings.telegram_message_limit
    assert crawler.pause_seconds == settings.telegram_rate_limit_pause_seconds


@respx.mock
def test_narrative_engine_crawls_and_clusters(session) -> None:
    asset = seed_market_asset(session)
    from common.config import get_settings

    settings = get_settings()
    settings.reddit_subreddits_csv = "CryptoMoonShots"
    settings.youtube_channels_csv = ""
    settings.github_search_queries_csv = ""
    settings.hf_trending_enabled = False
    settings.rss_feed_urls_csv = ""

    reddit_payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "HYPE token presale launching soon on Solana!",
                        "permalink": "/r/CryptoMoonShots/comments/aaa",
                        "selftext": "x100 gem do not miss it",
                        "author": "shillbot1",
                        "ups": 10,
                        "num_comments": 2,
                    }
                },
                {
                    "data": {
                        "title": "HYPE token presale launching soon on Solana!",
                        "permalink": "/r/CryptoMoonShots/comments/bbb",
                        "selftext": "gem",
                        "author": "shillbot2",
                        "ups": 5,
                        "num_comments": 1,
                    }
                },
            ]
        }
    }
    respx.get("https://www.reddit.com/r/CryptoMoonShots/new.json").mock(
        return_value=Response(200, json=reddit_payload)
    )

    engine = NarrativeEngine()
    counts = engine.crawl(session, decision_ts=NOW)
    session.commit()
    assert counts["reddit"] == 2

    mentions = session.scalars(select(models.SocialMention)).all()
    assert len(mentions) == 2
    assert all(mention.asset_id == asset.id for mention in mentions)

    clustered = engine.cluster(session, decision_ts=NOW)
    session.commit()
    assert clustered == 2
    assert session.scalar(select(func.count()).select_from(models.NarrativeCluster)) == 1
    keys = {
        (mention.metrics_json or {}).get("cluster_key")
        for mention in session.scalars(select(models.SocialMention)).all()
    }
    assert len(keys) == 1
    assert keys != {None}
