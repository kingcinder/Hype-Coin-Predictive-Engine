from __future__ import annotations

import time
from decimal import Decimal

import httpx
import pytest

from common.config import get_settings
from trading import gmgn_client
from trading.gmgn_client import GmgnReadClient, token_url


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(gmgn_client, "_blocked_until", 0.0)
    monkeypatch.setattr(gmgn_client, "_last_request_time", 0.0)
    monkeypatch.setattr(gmgn_client, "_gmgn_unconfigured_logged", False)
    monkeypatch.setattr(gmgn_client, "_price_cache", {})
    monkeypatch.setattr(gmgn_client, "_trending_cache", {})
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(gmgn_client.time, "sleep", lambda _s: None)


def _configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "gmgn_api_base", "https://gmgn.test")


class FakeHttp:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls: list[tuple] = []

    def get_json(self, path, *, params=None):
        self.calls.append((path, params))
        if self.exc is not None:
            raise self.exc
        return self.payload


def test_token_url_maps_chains() -> None:
    assert token_url("solana", "mint") == "https://gmgn.ai/sol/token/mint"
    assert token_url("base", "mint") == "https://gmgn.ai/base/token/mint"
    assert token_url("ethereum", "mint") == "https://gmgn.ai/eth/token/mint"
    with pytest.raises(ValueError):
        token_url("dogechain", "mint")


def test_unconfigured_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "gmgn_api_base", "")
    client = GmgnReadClient()
    client.http = FakeHttp({"price": "1"})
    assert client.price("mint", "solana") is None
    assert client.market_cap("mint", "solana") is None
    assert client.trending("solana") is None


def test_price_and_cache(monkeypatch) -> None:
    _configured(monkeypatch)
    client = GmgnReadClient()
    fake = FakeHttp({"price": "0.25"})
    client.http = fake

    quote = client.price("mint", "solana")
    assert quote is not None
    assert quote.price == Decimal("0.25")
    assert quote.source == "gmgn"

    again = client.price("mint", "solana")
    assert again is not None and again.price == Decimal("0.25")
    assert len(fake.calls) == 1  # served from the 10 s cache


def test_zero_or_missing_price_is_none(monkeypatch) -> None:
    _configured(monkeypatch)
    client = GmgnReadClient()
    client.http = FakeHttp({"price": "0"})
    assert client.price("mint", "solana") is None
    client.http = FakeHttp({"nope": 1})
    assert client.price("other", "solana") is None


def test_429_blocks_for_30s(monkeypatch) -> None:
    _configured(monkeypatch)
    request = httpx.Request("GET", "https://gmgn.test/token/price")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("429", request=request, response=response)
    fake = FakeHttp(exc=exc)
    client = GmgnReadClient()
    client.http = fake

    assert client.price("mint", "solana") is None
    assert client.price("mint", "solana") is None
    assert len(fake.calls) == 1  # second call short-circuits on the block


def test_rate_limit_five_calls(monkeypatch) -> None:
    _configured(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(gmgn_client.time, "sleep", lambda s: sleeps.append(s))
    client = GmgnReadClient()
    client.http = FakeHttp({"price": "1"})
    for i in range(5):
        client.price(f"mint{i}", "solana")
    assert len(sleeps) == 4  # one 0.5 s gap between each of the 5 calls
    assert min(sleeps) >= 0.49


def test_market_data_fields(monkeypatch) -> None:
    _configured(monkeypatch)
    client = GmgnReadClient()
    client.http = FakeHttp({"market_cap": "123", "liquidity": "45", "count": 7})
    assert client.market_cap("mint", "solana") == Decimal("123")
    assert client.liquidity("mint", "solana") == Decimal("45")
    assert client.holders("mint", "solana") == 7
