"""CoinMarketCap Night Crawler — new listings, trending tokens, market data.

With ``CMC_API_KEY`` configured the crawler uses the CoinMarketCap Pro API
(``pro-api``); without a key it keeps the legacy keyless ``data-api`` path.
Pro's dedicated trending/new-listing endpoints are plan-gated (403 on a Basic
key), so trending is taken from the highest-volume listings and new listings
from the ``date_added`` field of the same listings call. This crawler feeds
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
    """CoinMarketCap crawler — trending, listings, market data."""

    BASE_URL = "https://api.coinmarketcap.com/data-api/v3"
    PRO_BASE_URL = "https://pro-api.coinmarketcap.com"
    PRO_LISTINGS_PATH = "/v1/cryptocurrency/listings/latest"

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
        if self._api_key:
            return self._fetch_pro_items()
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_trending())
        items.extend(self._fetch_new_listings())
        items.extend(self._fetch_top_gainers())
        return items

    def _fetch_pro_items(self) -> list[dict[str, Any]]:
        """One Pro listings call, split into new, trending, and gaining coins."""
        try:
            resp = self.client.get(
                f"{self.PRO_BASE_URL}{self.PRO_LISTINGS_PATH}",
                params={"start": "1", "limit": "250", "convert": "USD"},
            )
            payload = resp.json()
        except Exception as exc:
            log.debug("cmc_pro_listings_failed", error=str(exc))
            return []
        coins = [coin for coin in (payload.get("data") or []) if isinstance(coin, dict)]
        if not coins:
            return []

        items: list[dict[str, Any]] = []
        newest = sorted(coins, key=lambda coin: str(coin.get("date_added") or ""), reverse=True)
        items.extend(self._pro_item(coin, "new_listing") for coin in newest[:50])
        by_volume = sorted(coins, key=self._pro_volume, reverse=True)
        items.extend(self._pro_item(coin, "trending") for coin in by_volume[:25])
        gainers = [
            coin
            for coin in coins
            if self._pro_change(coin) > 10 and self._pro_volume(coin) > 100_000
        ][:20]
        items.extend(self._pro_item(coin, "gainer") for coin in gainers)
        return items

    @staticmethod
    def _pro_quote(coin: dict[str, Any]) -> dict[str, Any]:
        quote = coin.get("quote")
        if not isinstance(quote, dict):
            return {}
        usd = quote.get("USD")
        return usd if isinstance(usd, dict) else {}

    @classmethod
    def _pro_number(cls, coin: dict[str, Any], key: str) -> float:
        try:
            return float(cls._pro_quote(coin).get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _pro_change(cls, coin: dict[str, Any]) -> float:
        return cls._pro_number(coin, "percent_change_24h")

    @classmethod
    def _pro_volume(cls, coin: dict[str, Any]) -> float:
        return cls._pro_number(coin, "volume_24h")

    def _pro_item(self, coin: dict[str, Any], kind: str) -> dict[str, Any]:
        symbol = str(coin.get("symbol") or "")
        name = str(coin.get("name") or symbol)
        slug = str(coin.get("slug") or "")
        change = self._pro_change(coin)
        metrics: dict[str, Any] = {
            "cmc_slug": slug,
            "symbol": symbol,
            "current_price": self._pro_number(coin, "price"),
            "market_cap": self._pro_number(coin, "market_cap"),
            "volume_24h": self._pro_number(coin, "volume_24h"),
            "market_cap_rank": coin.get("cmc_rank"),
            "date_added": coin.get("date_added"),
            "price_change_24h_pct": change,
        }
        if kind == "new_listing":
            text = f"{name} ({symbol}) listed on CoinMarketCap"
            metrics["new_listing"] = True
        elif kind == "gainer":
            text = f"{name} ({symbol}) up {change:.1f}% in 24h on CoinMarketCap"
            metrics["gainer"] = True
        else:
            text = f"{name} ({symbol}) trending on CoinMarketCap"
            metrics["trending"] = True
        return {
            "title": name,
            "text": text,
            "url": f"https://coinmarketcap.com/currencies/{slug}/",
            "published": utc_now(),
            "source_domain": "coinmarketcap.com",
            "source_type": "market_data",
            "metrics": metrics,
        }

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
