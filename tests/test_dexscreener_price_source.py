"""Tests for the keyless DexScreener live price source and the live-first composite."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading import price_sources
from trading.executors import PriceQuote
from trading.price_sources import (
    DexScreenerLivePriceSource,
    LiveFirstPriceSource,
)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(price_sources, "_price_cache", {})


class FakeHttp:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls: list[str] = []

    def get_json(self, path, *, params=None):
        self.calls.append(path)
        if self.exc is not None:
            raise self.exc
        return self.payload


class FakeFallback:
    def __init__(self, quote=None):
        self.quote = quote
        self.calls = 0

    def price(self, mint, chain):
        self.calls += 1
        return self.quote


def _pairs_payload():
    return {
        "pairs": [
            {
                "chainId": "solana",
                "priceUsd": "0.001",
                "liquidity": {"usd": 1000},
            },
            {
                "chainId": "solana",
                "priceUsd": "0.002",
                "liquidity": {"usd": 5000},
            },
            {
                "chainId": "base",
                "priceUsd": "9.99",
                "liquidity": {"usd": 999999},
            },
        ]
    }


def test_picks_highest_liquidity_pair_for_chain():
    source = DexScreenerLivePriceSource(http=FakeHttp(_pairs_payload()))
    quote = source.price("mint", "solana")
    assert quote is not None
    assert quote.price == Decimal("0.002")
    assert quote.source == "dexscreener_live"
    assert (datetime.now(UTC) - quote.ts).total_seconds() < 5


def test_unknown_chain_returns_none_without_fetch():
    fake = FakeHttp(_pairs_payload())
    source = DexScreenerLivePriceSource(http=fake)
    assert source.price("mint", "dogechain") is None
    assert fake.calls == []


def test_no_pair_for_chain_returns_none():
    payload = {"pairs": [{"chainId": "base", "priceUsd": "1", "liquidity": {"usd": 1}}]}
    source = DexScreenerLivePriceSource(http=FakeHttp(payload))
    assert source.price("mint", "solana") is None


def test_bad_and_zero_prices_skipped():
    payload = {
        "pairs": [
            {"chainId": "solana", "priceUsd": None, "liquidity": {"usd": 1}},
            {"chainId": "solana", "priceUsd": "not-a-number", "liquidity": {"usd": 2}},
            {"chainId": "solana", "priceUsd": "0", "liquidity": {"usd": 3}},
            {"chainId": "solana", "priceUsd": "0.5", "liquidity": {"usd": 4}},
        ]
    }
    source = DexScreenerLivePriceSource(http=FakeHttp(payload))
    quote = source.price("mint", "solana")
    assert quote is not None
    assert quote.price == Decimal("0.5")


def test_empty_and_malformed_payload_returns_none():
    for payload in (None, {}, {"pairs": None}, {"pairs": []}, "nope"):
        source = DexScreenerLivePriceSource(http=FakeHttp(payload))
        assert source.price("mint", "solana") is None


def test_http_error_returns_none():
    source = DexScreenerLivePriceSource(http=FakeHttp(exc=RuntimeError("boom")))
    assert source.price("mint", "solana") is None


def test_price_cached_within_ttl():
    fake = FakeHttp(_pairs_payload())
    source = DexScreenerLivePriceSource(http=fake)
    first = source.price("mint", "solana")
    second = source.price("mint", "solana")
    assert first is not None and second is not None
    assert first.price == second.price
    assert len(fake.calls) == 1


def test_live_first_prefers_live_quote():
    live = DexScreenerLivePriceSource(http=FakeHttp(_pairs_payload()))
    fallback = FakeFallback(PriceQuote(price=Decimal("7"), ts=datetime.now(UTC), source="snap"))
    composite = LiveFirstPriceSource(live, fallback)
    quote = composite.price("mint", "solana")
    assert quote is not None
    assert quote.source == "dexscreener_live"
    assert quote.price == Decimal("0.002")
    assert fallback.calls == 0


def test_live_first_falls_back_when_live_is_none():
    live = DexScreenerLivePriceSource(http=FakeHttp({"pairs": []}))
    fallback_quote = PriceQuote(price=Decimal("7"), ts=datetime.now(UTC), source="snap")
    fallback = FakeFallback(fallback_quote)
    composite = LiveFirstPriceSource(live, fallback)
    assert composite.price("mint", "solana") == fallback_quote
    assert fallback.calls == 1


def test_live_first_falls_back_when_live_raises():
    class ExplodingLive:
        def price(self, mint, chain):
            raise RuntimeError("boom")

    fallback_quote = PriceQuote(price=Decimal("7"), ts=datetime.now(UTC), source="snap")
    fallback = FakeFallback(fallback_quote)
    composite = LiveFirstPriceSource(ExplodingLive(), fallback)
    assert composite.price("mint", "solana") == fallback_quote
    assert fallback.calls == 1


def _bandana_cat_payload():
    """Live DexScreener shape for BANDANA CAT at the 18:17 PST check.

    The dust Meteora remnant ($2.22 liquidity, $1 mcap) has HIGHER 24h
    volume ($43,833) than the live Meteora DBC pair ($3,882) -- volume
    must not pick it. The live pair reports liquidity.usd null.
    """
    return {
        "pairs": [
            {
                "chainId": "solana",
                "dexId": "meteora",
                "pairAddress": "RhFte4X4gRykF2gFSimFKS7ty12ioSZBXdwZtKTVnLN",
                "priceUsd": "0.000000001326",
                "priceNative": "0.00000000001919",
                "liquidity": {"usd": 2.22, "base": 876637488, "quote": 0.01533},
                "volume": {"h24": 43833.3},
                "marketCap": 1,
                "fdv": 1,
            },
            {
                "chainId": "solana",
                "dexId": "meteoradbc",
                "pairAddress": "H4rFekyf18SAprok5PkzmPF9i8pWxP4KnnL5SN5hMNSu",
                "priceUsd": "0.0001916",
                "priceNative": "0.000002780",
                "liquidity": {"usd": None, "base": None, "quote": None},
                "volume": {"h24": 3882.06},
                "marketCap": 191662.18,
                "fdv": 191662.18,
            },
        ]
    }


def test_bandana_cat_picks_live_dbc_pair_over_dust_remnant():
    source = DexScreenerLivePriceSource(http=FakeHttp(_bandana_cat_payload()))
    quote = source.price("ExyVCqHzKrtu5EGNJLGrbdJBheeDmHstHnuv9h95m2Wt", "solana")
    assert quote is not None
    assert quote.price == Decimal("0.0001916")
    assert quote.price_native == Decimal("0.000002780")


def test_dust_only_pair_returns_none():
    payload = {"pairs": [_bandana_cat_payload()["pairs"][0]]}
    source = DexScreenerLivePriceSource(http=FakeHttp(payload))
    assert source.price("mint", "solana") is None


def test_null_liquidity_pair_ranked_by_mcap():
    payload = {
        "pairs": [
            {
                "chainId": "solana",
                "priceUsd": "0.001",
                "priceNative": "0.00001",
                "liquidity": {"usd": 500},
                "marketCap": 100,
            },
            {
                "chainId": "solana",
                "priceUsd": "0.002",
                "priceNative": "0.00002",
                "liquidity": {"usd": None},
                "marketCap": 50000,
            },
        ]
    }
    source = DexScreenerLivePriceSource(http=FakeHttp(payload))
    quote = source.price("mint", "solana")
    assert quote is not None
    assert quote.price == Decimal("0.002")
    assert quote.price_native == Decimal("0.00002")


def test_small_pool_with_big_mcap_is_not_dust():
    payload = {
        "pairs": [
            {
                "chainId": "solana",
                "priceUsd": "0.5",
                "priceNative": "0.005",
                "liquidity": {"usd": 5},
                "marketCap": 50000,
            },
        ]
    }
    source = DexScreenerLivePriceSource(http=FakeHttp(payload))
    quote = source.price("mint", "solana")
    assert quote is not None
    assert quote.price == Decimal("0.5")
