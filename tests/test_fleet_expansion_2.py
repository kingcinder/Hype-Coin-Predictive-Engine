"""Tests for the round-2 fleet expansion crawlers (pump_portal,
dexscreener_trends, google_trends)."""

from __future__ import annotations

import json

import respx
from httpx import Response

from crawlers.sources.dexscreener_trends import DexScreenerTrendsCrawler
from crawlers.sources.google_trends import GoogleTrendsCrawler
from crawlers.sources.pump_portal import PumpPortalCrawler


@respx.mock
class _FakePumpWS:
    """Minimal stand-in for a websockets.sync.client connection."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)

    def __enter__(self) -> _FakePumpWS:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def send(self, _msg: str) -> None:
        return None

    def recv(self, timeout: float | None = None) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise TimeoutError("no more messages")


def _pump_create_event(*, mint: str, symbol: str, name: str) -> str:
    return json.dumps(
        {
            "txType": "create",
            "mint": mint,
            "traderPublicKey": "DEPLOYER111111111111111111111111111111111111",
            "signature": "sig1",
            "initialBuy": 1.5,
            "bondingCurveKey": "curve1",
            "marketCapSol": 250.0,
            "name": name,
            "symbol": symbol,
        }
    )


def test_pump_portal_parses_ws_tap_events(monkeypatch) -> None:
    from unittest.mock import patch

    messages = [
        json.dumps({"message": "Successfully subscribed to token creation events."}),
        _pump_create_event(
            mint="MINTADDRESS11111111111111111111111111111111", symbol="HDOG", name="Hype Dog"
        ),
        _pump_create_event(
            mint="SECONDMINT222222222222222222222222222222222222", symbol="BOR", name="Boring Coin"
        ),
    ]
    monkeypatch.setattr("crawlers.sources.pump_portal.WS_TAP_SECONDS", 0.5)
    with patch("websockets.sync.client.connect", return_value=_FakePumpWS(messages)):
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


def test_pump_portal_ws_failure_returns_empty() -> None:
    from unittest.mock import patch

    with patch("websockets.sync.client.connect", side_effect=ConnectionError("no net")):
        crawler = PumpPortalCrawler()
        try:
            items = crawler.fetch()
        finally:
            crawler.close()
    assert items == []


def test_pump_portal_skips_invalid_rows(monkeypatch) -> None:
    from unittest.mock import patch

    messages = [
        json.dumps(
            {"txType": "create", "mint": "MINTADDRESS11111111111111111111111111111111"}
        ),  # no symbol
        json.dumps({"txType": "create", "symbol": "NOSYM"}),  # no mint
        _pump_create_event(
            mint="OKMINT33333333333333333333333333333333333333", symbol="OK", name="OK Coin"
        ),
    ]
    monkeypatch.setattr("crawlers.sources.pump_portal.WS_TAP_SECONDS", 0.5)
    with patch("websockets.sync.client.connect", return_value=_FakePumpWS(messages)):
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
