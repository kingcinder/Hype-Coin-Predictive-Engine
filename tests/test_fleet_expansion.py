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
    crawler = GasTrackerCrawler(include_solana=False, include_pending_tx=False)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert items == []


@respx.mock
def test_gas_tracker_pending_tx_severe_congestion() -> None:
    """Pending tx count above 200k = severe congestion signal."""
    # Mock the Etherscan oracle (gas prices normal)
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
    # Mock the txpool_status RPC
    respx.post("https://ethereum-rpc.publicnode.com").mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "pending": "0x3e800",  # 256,000 in decimal
                    "queued": "0x1e848",  # 125,000 in decimal
                },
            },
        )
    )
    crawler = GasTrackerCrawler(include_solana=False, include_pending_tx=True)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    # 1 gas oracle + 1 pending tx = 2 items
    assert len(items) == 2
    pending_item = items[1]
    assert pending_item["source_domain"] == "ethereum-rpc"
    assert pending_item["source_type"] == "market_data"
    assert pending_item["metrics"]["signal"] == "pending_tx_congestion"
    assert pending_item["metrics"]["pending_txs"] == 256_000
    assert pending_item["metrics"]["queued_txs"] == 125_000
    assert pending_item["metrics"]["total_txs"] == 381_000
    assert pending_item["metrics"]["congestion"] == "severe"
    assert pending_item["metrics"]["congested"] is True
    assert "severe" in pending_item["title"]


@respx.mock
def test_gas_tracker_pending_tx_high_congestion() -> None:
    """Pending tx count above 80k but below 200k = high congestion."""
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
    respx.post("https://ethereum-rpc.publicnode.com").mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "pending": "0x186a0",  # 100,000 in decimal
                    "queued": "0x2710",  # 10,000 in decimal
                },
            },
        )
    )
    crawler = GasTrackerCrawler(include_solana=False, include_pending_tx=True)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 2
    pending_item = items[1]
    assert pending_item["metrics"]["pending_txs"] == 100_000
    assert pending_item["metrics"]["congestion"] == "high"
    assert pending_item["metrics"]["congested"] is True


@respx.mock
def test_gas_tracker_pending_tx_normal_congestion() -> None:
    """Pending tx count below 80k = normal congestion."""
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
    respx.post("https://ethereum-rpc.publicnode.com").mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "pending": "0x1388",  # 5,000 in decimal
                    "queued": "0x3e8",  # 1,000 in decimal
                },
            },
        )
    )
    crawler = GasTrackerCrawler(include_solana=False, include_pending_tx=True)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 2
    pending_item = items[1]
    assert pending_item["metrics"]["pending_txs"] == 5_000
    assert pending_item["metrics"]["congestion"] == "normal"
    assert pending_item["metrics"]["congested"] is False


@respx.mock
def test_gas_tracker_pending_tx_fallback_on_rpc_failure() -> None:
    """When the first RPC fails, try the next one."""
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
    # First RPC fails
    respx.post("https://ethereum-rpc.publicnode.com").mock(
        return_value=Response(500, json={"error": "server error"})
    )
    # Second RPC succeeds
    respx.post("https://eth.llamarpc.com").mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "pending": "0x186a0",  # 100,000
                    "queued": "0x2710",  # 10,000
                },
            },
        )
    )
    crawler = GasTrackerCrawler(include_solana=False, include_pending_tx=True)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 2
    pending_item = items[1]
    assert pending_item["metrics"]["pending_txs"] == 100_000
    assert pending_item["metrics"]["rpc_source"] == "eth.llamarpc.com"


@respx.mock
def test_gas_tracker_pending_tx_all_rpcs_fail() -> None:
    """When all RPCs fail, pending tx item is skipped but gas oracle still works."""
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
    # All RPCs fail
    respx.post("https://ethereum-rpc.publicnode.com").mock(
        return_value=Response(500, json={"error": "server error"})
    )
    respx.post("https://eth.llamarpc.com").mock(
        return_value=Response(500, json={"error": "server error"})
    )
    respx.post("https://rpc.ankr.com/eth").mock(
        return_value=Response(500, json={"error": "server error"})
    )
    crawler = GasTrackerCrawler(include_solana=False, include_pending_tx=True)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    # Only gas oracle item, no pending tx
    assert len(items) == 1
    assert items[0]["metrics"]["chain"] == "ethereum"


@respx.mock
def test_gas_tracker_pending_tx_disabled() -> None:
    """When include_pending_tx=False, no pending tx item is returned."""
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
    crawler = GasTrackerCrawler(include_solana=False, include_pending_tx=False)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    # Only gas oracle item
    assert len(items) == 1


@respx.mock
def test_gas_tracker_pending_tx_bad_rpc_response() -> None:
    """RPC returns non-dict result — skip this endpoint, try next."""
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
    # First RPC returns non-dict result
    respx.post("https://ethereum-rpc.publicnode.com").mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": "NOTOK",
            },
        )
    )
    # Second RPC returns valid response
    respx.post("https://eth.llamarpc.com").mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "pending": "0x1388",  # 5,000
                    "queued": "0x3e8",  # 1,000
                },
            },
        )
    )
    crawler = GasTrackerCrawler(include_solana=False, include_pending_tx=True)
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 2
    pending_item = items[1]
    assert pending_item["metrics"]["pending_txs"] == 5_000
    assert pending_item["metrics"]["rpc_source"] == "eth.llamarpc.com"


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
