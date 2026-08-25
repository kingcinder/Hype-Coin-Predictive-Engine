"""CoinMarketCap Night Crawler — new listings, trending tokens, market data.

Uses the CoinMarketCap free API (no key required for basic endpoints) to track
new token listings, trending tokens, and market cap data. This crawler feeds
the signal scoring engine with market intelligence from a second major source,
providing corroboration alongside CoinGecko data.
"""

from __future__ import annotations

from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler

log = get_logger(__name__)


class CMCCrawler(BaseCrawler):
    """CoinMarketCap free API crawler — trending, listings, market data."""

    BASE_URL = "https://api.coinmarketcap.com/data-api/v3"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        super().__init__(
            name="coinmarketcap",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=2.0,
            timeout_seconds=20.0,
        )

    def _create_client_headers(self) -> dict[str, str]:
        """Inject the CMC API key header when provided."""
        headers = super()._create_client_headers()
        if self._api_key:
            headers["X-CMC_PRO_API_KEY"] = self._api_key
        return headers

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_trending())
        items.extend(self._fetch_new_listings())
        items.extend(self._fetch_top_gainers())
        return items

    def _fetch_trending(self) -> list[dict[str, Any]]:
        """Fetch trending cryptocurrencies from CMC."""
        try:
            trending_data = self.client.get(
                f"{self.BASE_URL}/cryptocurrency/trending",
                params={"convert": "USD"},
            ).json()

            items: list[dict[str, Any]] = []
            trending_list: list[dict[str, Any]] = (
                trending_data.get("data", {}).get("cryptoCurrency", [])
                if isinstance(trending_data.get("data"), dict)
                else []
            )

            for coin in trending_list[:25]:
                symbol = coin.get("symbol", "")
                name = coin.get("name", "")
                slug = coin.get("slug", "")
                items.append(
                    {
                        "title": name,
                        "text": f"{name} ({symbol}) trending on CoinMarketCap",
                        "url": f"https://coinmarketcap.com/currencies/{slug}/",
                        "published": utc_now(),
                        "source_domain": "coinmarketcap.com",
                        "source_type": "market_data",
                        "metrics": {
                            "cmc_slug": slug,
                            "symbol": symbol,
                            "market_cap_rank": coin.get("cmcRank"),
                            "trending": True,
                        },
                    }
                )
            return items
        except Exception as exc:
            log.debug("cmc_trending_failed", error=str(exc))
            return []

    def _fetch_new_listings(self) -> list[dict[str, Any]]:
        """Fetch recently added cryptocurrencies from CMC."""
        try:
            resp = self.client.get(
                f"{self.BASE_URL}/cryptocurrency/listings/latest",
                params={
                    "start": "1",
                    "limit": "50",
                    "sort": "date_added",
                    "sort_dir": "desc",
                    "convert": "USD",
                },
            )
            data = resp.json()

            items: list[dict[str, Any]] = []
            listings: list[dict[str, Any]] = (
                data.get("data", {}).get("cryptoCurrencyList", [])
                if isinstance(data.get("data"), dict)
                else []
            )

            for coin in listings:
                symbol = coin.get("symbol", "")
                name = coin.get("name", "")
                slug = coin.get("slug", "")
                quotes = coin.get("quotes") or []
                quote = quotes[0] if quotes else {}
                price = quote.get("price", 0) if quote else 0

                items.append(
                    {
                        "title": name,
                        "text": f"{name} ({symbol}) listed on CoinMarketCap",
                        "url": f"https://coinmarketcap.com/currencies/{slug}/",
                        "published": utc_now(),
                        "source_domain": "coinmarketcap.com",
                        "source_type": "market_data",
                        "metrics": {
                            "cmc_slug": slug,
                            "symbol": symbol,
                            "current_price": price,
                            "market_cap": coin.get("marketCap"),
                            "volume_24h": coin.get("volume"),
                            "market_cap_rank": coin.get("cmcRank"),
                            "date_added": coin.get("dateAdded"),
                            "new_listing": True,
                        },
                    }
                )
            return items
        except Exception as exc:
            log.debug("cmc_listings_failed", error=str(exc))
            return []

    def _fetch_top_gainers(self) -> list[dict[str, Any]]:
        """Fetch top gainers (coins with highest 24h price increase) from CMC."""
        try:
            resp = self.client.get(
                f"{self.BASE_URL}/cryptocurrency/listings/latest",
                params={
                    "start": "1",
                    "limit": "100",
                    "sort": "percent_change_24h",
                    "sort_dir": "desc",
                    "convert": "USD",
                },
            )
            data = resp.json()

            items: list[dict[str, Any]] = []
            listings: list[dict[str, Any]] = (
                data.get("data", {}).get("cryptoCurrencyList", [])
                if isinstance(data.get("data"), dict)
                else []
            )

            gainers = [
                c
                for c in listings
                if (c.get("percentChange24h") or 0) > 10 and (c.get("volume") or 0) > 100_000
            ][:20]

            for coin in gainers:
                symbol = coin.get("symbol", "")
                name = coin.get("name", "")
                slug = coin.get("slug", "")
                pct = coin.get("percentChange24h") or 0
                items.append(
                    {
                        "title": name,
                        "text": f"{name} ({symbol}) up {pct:.1f}% in 24h on CoinMarketCap",
                        "url": f"https://coinmarketcap.com/currencies/{slug}/",
                        "published": utc_now(),
                        "source_domain": "coinmarketcap.com",
                        "source_type": "market_data",
                        "metrics": {
                            "cmc_slug": slug,
                            "symbol": symbol,
                            "current_price": coin.get("price"),
                            "market_cap": coin.get("marketCap"),
                            "volume_24h": coin.get("volume"),
                            "price_change_24h_pct": pct,
                            "market_cap_rank": coin.get("cmcRank"),
                            "gainer": True,
                        },
                    }
                )
            return items
        except Exception as exc:
            log.debug("cmc_gainers_failed", error=str(exc))
            return []
