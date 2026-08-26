"""Tests for the round-2 fleet expansion crawlers (pump_portal,
dexscreener_trends, google_trends)."""

from __future__ import annotations

import respx
from httpx import Response

from crawlers.sources.dexscreener_trends import DexScreenerTrendsCrawler
from crawlers.sources.google_trends import GoogleTrendsCrawler
from crawlers.sources.pump_portal import PumpPortalCrawler


@respx.mock
def test_pump_portal_parses_http_recent() -> None:
    respx.get("https://api.pumpportal.io/pumps/recent").mock(
        return_value=Response(
            200,
            json=[
                {
                    "signature": "sig1",
                    "mint": "MINTADDRESS11111111111111111111111111111111",
                    "traderPublicKey": "DEPLOYER111111111111111111111111111111111111",
                    "txType": "create",
                    "initialBuy": 1.5,
                    "bondingCurveKey": "curve1",
                    "marketCapSol": 250.0,
                    "name": "Hype Dog",
                    "symbol": "HDOG",
                    "uri": "https://ipfs.io/hdog",
                },
                {
                    "signature": "sig2",
                    "mint": "SECONDMINT222222222222222222222222222222222222",
                    "traderPublicKey": "DEPLOYER222222222222222222222222222222222222",
                    "txType": "create",
                    "initialBuy": 0.0,
                    "name": "Boring Coin",
                    "symbol": "BOR",
                },
            ],
        )
    )
    crawler = PumpPortalCrawler()
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 2
    first = items[0]
    assert first["source_domain"] == "pump.fun"
    assert first["metrics"]["symbol"] == "HDOG"
    assert first["metrics"]["new_token"] is True
    assert first["metrics"]["initial_buy_sol"] == 1.5
    assert "pump.fun/coin" in first["url"]


@respx.mock
def test_pump_portal_http_empty_falls_back_to_ws() -> None:
    # HTTP endpoint returns 500 -> the crawler should fall back to the WS tap.
    # The WS connect is patched to fail so the test never touches the network.
    from unittest.mock import patch

    respx.get("https://api.pumpportal.io/pumps/recent").mock(
        return_value=Response(500, text="boom")
    )
    crawler = PumpPortalCrawler()
    try:
        with patch("websockets.sync.client.connect", side_effect=ConnectionError("no net")):
            items = crawler.fetch()
    finally:
        crawler.close()
    assert items == []


@respx.mock
def test_pump_portal_skips_invalid_rows() -> None:
    respx.get("https://api.pumpportal.io/pumps/recent").mock(
        return_value=Response(
            200,
            json=[
                {"mint": "MINTADDRESS11111111111111111111111111111111"},  # no symbol
                {"symbol": "NOSYM"},  # no mint
                {
                    "mint": "OKMINT33333333333333333333333333333333333333",
                    "symbol": "OK",
                    "name": "OK Coin",
                },
            ],
        )
    )
    crawler = PumpPortalCrawler()
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 1
    assert items[0]["metrics"]["symbol"] == "OK"


@respx.mock
def test_dexscreener_trends_parses_profiles_and_boosts() -> None:
    respx.get("https://api.dexscreener.com/token-profiles/latest/v1").mock(
        return_value=Response(
            200,
            json=[
                {
                    "url": "https://dexscreener.com/solana/ADDR1",
                    "chainId": "solana",
                    "tokenAddress": "ADDR1",
                    "description": "New memecoin launch on Solana",
                    "links": [{"type": "twitter", "label": "x", "url": "https://x.com/foo"}],
                }
            ],
        )
    )
    respx.get("https://api.dexscreener.com/token-boosts/top/v1").mock(
        return_value=Response(
            200,
            json=[
                {
                    "url": "https://dexscreener.com/base/ADDR2",
                    "chainId": "base",
                    "tokenAddress": "ADDR2",
                    "amount": 100,
                    "totalAmount": 500,
                }
            ],
        )
    )
    crawler = DexScreenerTrendsCrawler()
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert len(items) == 2
    profile, boost = items
    assert profile["metrics"]["new_profile"] is True
    assert profile["metrics"]["chain"] == "solana"
    assert boost["metrics"]["boosted"] is True
    assert boost["metrics"]["boost_total"] == 500
    assert boost["metrics"]["chain"] == "base"


@respx.mock
def test_google_trends_strips_jsonp_and_filters_crypto() -> None:
    jsonp = (
        ")]}',\n"
        + '{"initialData":{"data":{"realtrends":['
        + '{"title":{"query":"bitcoin price crash"},"formattedTraffic":"200K+"},'
        + '{"title":{"query":"world cup final"},"formattedTraffic":"2M+"},'
        + '{"title":{"query":"solana airdrop claim"},"formattedTraffic":"100K+"}'
        + "]}}}"
    )
    respx.get("https://trends.google.com/trends/api/realtimetrends").mock(
        return_value=Response(200, text=jsonp)
    )
    crawler = GoogleTrendsCrawler()
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    # "world cup final" has no crypto keyword -> dropped.
    assert len(items) == 2
    queries = {item["metrics"]["query"] for item in items}
    assert queries == {"bitcoin price crash", "solana airdrop claim"}
    assert items[0]["source_domain"] == "google.com"
    assert items[0]["source_type"] == "news"


@respx.mock
def test_google_trends_handles_bad_jsonp() -> None:
    respx.get("https://trends.google.com/trends/api/realtimetrends").mock(
        return_value=Response(200, text=")]}',\nnot json at all")
    )
    crawler = GoogleTrendsCrawler()
    try:
        items = crawler.fetch()
    finally:
        crawler.close()
    assert items == []


def test_google_trends_parse_jsonp_edge_cases() -> None:
    assert GoogleTrendsCrawler._parse_jsonp(")]}',\n{}") == {}
    assert GoogleTrendsCrawler._parse_jsonp('{"a": 1}') == {"a": 1}
    assert GoogleTrendsCrawler._parse_jsonp("garbage") is None
