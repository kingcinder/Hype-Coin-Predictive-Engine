"""Tests for the Phase 8 fleet expansion crawlers (gas_tracker, coinpaprika,
github_trending, x_trends)."""

from __future__ import annotations

import respx
from httpx import Response

from crawlers.sources.coinpaprika import CoinPaprikaCrawler
from crawlers.sources.gas_tracker import GasTrackerCrawler
from crawlers.sources.github_trending import GitHubTrendingCrawler
from crawlers.sources.x_trends import XTrendsCrawler


@respx.mock
def test_gas_tracker_parses_eth_oracle() -> None:
    respx.get("https://api.etherscan.io/api").mock(
        return_value=Response(
            200,
            json={
                "status": "1",
                "result": {
                    "SafeGasPrice": "12",
                    "ProposeGasPrice": "15",
                    "FastGasPrice": "125",
                    "suggestBaseFee": "11.5",
                },
            },
        )
    )
    crawler = GasTrackerCrawler(include_solana=False)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 1
    item = items[0]
    assert item["source_domain"] == "etherscan.io"
    assert item["metrics"]["chain"] == "ethereum"
    assert item["metrics"]["fast_gwei"] == 125.0
    assert item["metrics"]["regime"] == "spike"
    assert item["metrics"]["gas_spike"] is True


@respx.mock
def test_gas_tracker_normal_regime_no_spike() -> None:
    respx.get("https://api.etherscan.io/api").mock(
        return_value=Response(
            200,
            json={
                "status": "1",
                "result": {
                    "SafeGasPrice": "5",
                    "ProposeGasPrice": "7",
                    "FastGasPrice": "9",
                    "suggestBaseFee": "4.5",
                },
            },
        )
    )
    crawler = GasTrackerCrawler(include_solana=False)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert items[0]["metrics"]["regime"] == "normal"
    assert items[0]["metrics"]["gas_spike"] is False


@respx.mock
def test_gas_tracker_handles_bad_oracle_response() -> None:
    # Oracle returns a non-dict "result" (e.g. rate-limited NOTOK) — must not raise.
    respx.get("https://api.etherscan.io/api").mock(
        return_value=Response(200, json={"status": "0", "result": "NOTOK"})
    )
    crawler = GasTrackerCrawler(include_solana=False)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert items == []


@respx.mock
def test_coinpaprika_parses_gainers() -> None:
    respx.get("https://api.coinpaprika.com/v1/tickers").mock(
        return_value=Response(
            200,
            json=[
                {
                    "id": "hype-hype",
                    "name": "HYPE Token",
                    "symbol": "HYPE",
                    "rank": 12,
                    "quotes": {
                        "USD": {
                            "price": 0.42,
                            "market_cap": 4_200_000,
                            "volume_24h": 1_200_000,
                            "percent_change_24h": 42.0,
                        }
                    },
                },
                {
                    "id": "boring-boring",
                    "name": "Boring",
                    "symbol": "BOR",
                    "rank": 900,
                    "quotes": {
                        "USD": {
                            "price": 0.001,
                            "market_cap": 10_000,
                            "volume_24h": 500,
                            "percent_change_24h": 2.0,
                        }
                    },
                },
            ],
        )
    )
    respx.get("https://api.coinpaprika.com/v1/global").mock(
        return_value=Response(
            200,
            json={
                "quotes": {
                    "USD": {
                        "total_market_cap": 2_500_000_000_000,
                        "volume_24h": 95_000_000_000,
                        "bitcoin_dominance_percentage": 54.2,
                    }
                }
            },
        )
    )
    crawler = CoinPaprikaCrawler()
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    # 1 gainer (HYPE, 42% up & liquid) + 1 global snapshot
    assert len(items) == 2
    gainer = items[0]
    assert gainer["metrics"]["gainer"] is True
    assert gainer["metrics"]["symbol"] == "HYPE"
    assert gainer["metrics"]["price_change_24h_pct"] == 42.0
    assert items[1]["metrics"]["global_snapshot"] is True


@respx.mock
def test_github_trending_parses_search_results() -> None:
    respx.get("https://api.github.com/search/repositories").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    {
                        "full_name": "alice/hype-token",
                        "description": "A new memecoin token with smart contract",
                        "html_url": "https://github.com/alice/hype-token",
                        "stargazers_count": 5,
                        "forks_count": 2,
                        "language": "Solidity",
                        "topics": ["token", "memecoin"],
                        "created_at": "2026-08-01T00:00:00Z",
                        "pushed_at": "2026-08-25T00:00:00Z",
                    },
                    {
                        "full_name": "bob/random-utils",
                        "description": "Utility library",
                        "html_url": "https://github.com/bob/random-utils",
                        "stargazers_count": 1,
                        "forks_count": 0,
                        "language": "Python",
                        "topics": [],
                        "created_at": "2026-07-01T00:00:00Z",
                        "pushed_at": "2026-08-20T00:00:00Z",
                    },
                ]
            },
        )
    )
    crawler = GitHubTrendingCrawler()
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 2
    assert items[0]["source_domain"] == "github.com"
    assert items[0]["source_type"] == "developer_activity"
    assert items[0]["metrics"]["launch_relevance"] >= 2
    assert items[0]["metrics"]["new_repo"] is True


@respx.mock
def test_github_trending_non_200_returns_empty() -> None:
    respx.get("https://api.github.com/search/repositories").mock(
        return_value=Response(403, json={"message": "rate limit"})
    )
    crawler = GitHubTrendingCrawler()
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert items == []


def _rss_feed(items: list[tuple[str, str]]) -> str:
    """Build a minimal RSS 2.0 feed from (title, description) pairs."""
    entry_xml = "".join(
        f"<item><title>{title}</title>"
        f"<link>https://nitter.privacydev.net/user/{i}</link>"
        f"<description>{description}</description></item>"
        for i, (title, description) in enumerate(items)
    )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0'><channel>"
        f"{entry_xml}"
        "</channel></rss>"
    )


@respx.mock
def test_x_trends_ranks_keywords_by_volume() -> None:
    instance = "https://nitter.privacydev.net"
    # "solana" feed: only the $SOL tweet parses (other lacks a token mention),
    # so count=1 < TREND_MIN_TWEETS=3 -> no trend emitted.
    rss_sol = _rss_feed(
        [
            ("solana token $SOL rocket today", "check this solana token out"),
            ("solana airdrop claim now", "solana airdrop is live"),
        ]
    )
    rss_btc = _rss_feed([])

    def _handler(request):
        q = request.url.params.get("q", "")
        if "solana" in q:
            return Response(200, text=rss_sol)
        return Response(200, text=rss_btc)

    respx.get(f"{instance}/search/rss").mock(side_effect=_handler)

    crawler = XTrendsCrawler(keywords=["solana", "btc"])
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert items == []


@respx.mock
def test_x_trends_emits_above_threshold() -> None:
    instance = "https://nitter.privacydev.net"
    # 4 fresh tweets each carrying a $AIR mention and 20+ char text.
    rss_air = _rss_feed(
        [
            ("airdrop $AIR claim round 0 is live today", "claim $AIR airdrop round 0 now"),
            ("airdrop $AIR claim round 1 is live today", "claim $AIR airdrop round 1 now"),
            ("airdrop $AIR claim round 2 is live today", "claim $AIR airdrop round 2 now"),
            ("airdrop $AIR claim round 3 is live today", "claim $AIR airdrop round 3 now"),
        ]
    )
    rss_btc = _rss_feed([("btc quietly moving today", "btc small move, nothing special")])

    def _handler(request):
        q = request.url.params.get("q", "")
        if "airdrop" in q:
            return Response(200, text=rss_air)
        return Response(200, text=rss_btc)

    respx.get(f"{instance}/search/rss").mock(side_effect=_handler)

    crawler = XTrendsCrawler(keywords=["airdrop", "btc"])
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 1
    assert items[0]["metrics"]["keyword"] == "airdrop"
    assert items[0]["metrics"]["tweet_count"] == 4
    assert items[0]["metrics"]["rank"] == 1
    assert items[0]["source_type"] == "social"
