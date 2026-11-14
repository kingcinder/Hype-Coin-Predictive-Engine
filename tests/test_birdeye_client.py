from __future__ import annotations

import httpx
import respx

from ingestion.birdeye_client import BirdeyeClient

BASE = "https://public-api.birdeye.so"


def _client() -> BirdeyeClient:
    return BirdeyeClient(api_key="test-key")


def test_token_overview_uses_live_path_and_parses_snapshot() -> None:
    client = _client()
    try:
        with respx.mock:
            route = respx.get(f"{BASE}/defi/token_overview").mock(
                return_value=httpx.Response(
                    200,
                    json={"success": True, "data": {"address": "Mint1", "price": 1.5}},
                )
            )
            snapshot = client.token_overview("Mint1")
        assert snapshot == {"address": "Mint1", "price": 1.5}
        request = route.calls[0].request
        assert request.url.params["address"] == "Mint1"
        assert request.headers["X-API-KEY"] == "test-key"
        assert request.headers["x-chain"] == "solana"
    finally:
        client.close()


def test_trending_tokens_uses_token_trending_path() -> None:
    client = _client()
    try:
        with respx.mock:
            route = respx.get(f"{BASE}/defi/token_trending").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"tokens": [{"address": "A", "rank": 1}]},
                    },
                )
            )
            tokens = client.trending_tokens(limit=35)
        assert tokens == [{"address": "A", "rank": 1}]
        request = route.calls[0].request
        assert request.url.params["sort_by"] == "rank"
        assert request.url.params["sort_type"] == "asc"
        assert request.url.params["offset"] == "0"
        # The live endpoint caps a page at 20 tokens.
        assert request.url.params["limit"] == "20"
    finally:
        client.close()


def test_top_gainers_uses_token_list_price_change_sort() -> None:
    client = _client()
    try:
        with respx.mock:
            route = respx.get(f"{BASE}/defi/v3/token/list").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"items": [{"address": "B", "price_change_24h_percent": 42.0}]},
                    },
                )
            )
            gainers = client.top_gainers(limit=10)
        assert gainers == [{"address": "B", "price_change_24h_percent": 42.0}]
        request = route.calls[0].request
        assert request.url.params["sort_by"] == "price_change_24h_percent"
        assert request.url.params["sort_type"] == "desc"
        assert request.url.params["limit"] == "10"
    finally:
        client.close()


def test_failed_payloads_return_empty_lists() -> None:
    client = _client()
    try:
        with respx.mock:
            respx.get(f"{BASE}/defi/token_trending").mock(
                return_value=httpx.Response(200, json={"success": False, "data": {}})
            )
            respx.get(f"{BASE}/defi/v3/token/list").mock(
                return_value=httpx.Response(200, json={"success": True, "data": {}})
            )
            assert client.trending_tokens() == []
            assert client.top_gainers() == []
    finally:
        client.close()
