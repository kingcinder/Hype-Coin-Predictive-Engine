from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from crawlers.sources.cmc import CMCCrawler


def _coin(
    *,
    symbol: str,
    slug: str,
    name: str,
    date_added: str,
    volume: float,
    change: float,
    rank: int,
) -> dict[str, Any]:
    return {
        "name": name,
        "symbol": symbol,
        "slug": slug,
        "date_added": date_added,
        "cmc_rank": rank,
        "quote": {
            "USD": {
                "price": 2.5,
                "market_cap": 1_000_000,
                "volume_24h": volume,
                "percent_change_24h": change,
            }
        },
    }


PRO_PAYLOAD = {
    "data": [
        _coin(
            symbol="OLD",
            slug="old",
            name="Old Coin",
            date_added="2026-11-01T00:00:00.000Z",
            volume=200_000,
            change=15,
            rank=3,
        ),
        _coin(
            symbol="NEW",
            slug="new",
            name="New Coin",
            date_added="2026-11-13T00:00:00.000Z",
            volume=1_000,
            change=1,
            rank=50,
        ),
        _coin(
            symbol="HOT",
            slug="hot",
            name="Hot Coin",
            date_added="2026-11-10T00:00:00.000Z",
            volume=5_000_000,
            change=25,
            rank=7,
        ),
    ]
}


class _FakeClient:
    is_closed = False

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((url, params))
        return SimpleNamespace(json=lambda: self.payload)


def test_pro_key_uses_pro_api_and_parses_listings() -> None:
    """A configured key must reach the Pro API, not the dead free host."""
    crawler = CMCCrawler(api_key="test-cmc-key")
    fake = _FakeClient(PRO_PAYLOAD)
    crawler._client = fake  # type: ignore[assignment]

    items = crawler.fetch_items()

    assert fake.calls
    url, params = fake.calls[0]
    assert url == "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    assert params == {"start": "1", "limit": "250", "convert": "USD"}
    assert crawler._create_client_headers()["X-CMC_PRO_API_KEY"] == "test-cmc-key"

    kinds = {
        kind
        for item in items
        for kind in ("new_listing", "trending", "gainer")
        if item["metrics"].get(kind)
    }
    assert kinds == {"new_listing", "trending", "gainer"}

    newest = [item for item in items if item["metrics"].get("new_listing")]
    assert [item["metrics"]["symbol"] for item in newest] == ["NEW", "HOT", "OLD"]

    trending = [item for item in items if item["metrics"].get("trending")]
    assert trending[0]["metrics"]["symbol"] == "HOT"

    gainers = [item for item in items if item["metrics"].get("gainer")]
    assert {item["metrics"]["symbol"] for item in gainers} == {"HOT", "OLD"}
    hot = next(item for item in gainers if item["metrics"]["symbol"] == "HOT")
    assert "up 25.0% in 24h" in hot["text"]
    assert hot["metrics"]["market_cap_rank"] == 7
    assert hot["metrics"]["current_price"] == 2.5
    assert hot["url"] == "https://coinmarketcap.com/currencies/hot/"


def test_without_key_keeps_legacy_free_path() -> None:
    crawler = CMCCrawler()
    fake = _FakeClient({"data": {}})
    crawler._client = fake  # type: ignore[assignment]

    assert crawler.fetch_items() == []

    assert fake.calls
    assert all("api.coinmarketcap.com/data-api" in url for url, _ in fake.calls)
