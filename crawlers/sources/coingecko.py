"""CoinGecko Night Crawler — market data, trending tokens, historical prices.

Pulls trending tokens, new listings, market cap data, and historical OHLC
from the CoinGecko free API. This crawler provides the market intelligence
that feeds the signal scoring engine and label densification.
"""
from __future__ import annotations

from typing import Any

from common.time import utc_now
from crawlers.base import BaseCrawler


class CoinGeckoCrawler(BaseCrawler):
    """CoinGecko free API crawler — trending, new listings, market data."""

    BASE_URL = "https://api.coingecko.com/api/v3"

    def __init__(self) -> None:
        super().__init__(
            name="coingecko",
            max_retries=2,
            retry_delay_seconds=5.0,  # CoinGecko rate limits aggressively
            rate_limit_pause=2.0,
            timeout_seconds=20.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_trending())
        items.extend(self._fetch_new_listings())
        items.extend(self._fetch_top_gainers())
        return items

    def _fetch_trending(self) -> list[dict[str, Any]]:
        """Fetch trending coins from CoinGecko."""
        try:
            data = self.client.get(f"{self.BASE_URL}/search/trending").json()
            items = []
            for coin in (data.get("coins") or [])[:25]:
                item = coin.get("item") or {}
                items.append({
                    "title": item.get("name", ""),
                    "text": f"{item.get('name', '')} {item.get('symbol', '')} trending on CoinGecko",
                    "url": f"https://www.coingecko.com/en/coins/{item.get('id', '')}",
                    "published": utc_now(),
                    "source_domain": "coingecko.com",
                    "source_type": "market_data",
                    "metrics": {
                        "coingecko_id": item.get("id"),
                        "symbol": item.get("symbol"),
                        "market_cap_rank": item.get("market_cap_rank"),
                        "score": item.get("score", 0),
                        "price_btc": item.get("price_btc"),
                        "trending": True,
                    },
                })
            return items
        except Exception:
            return []

    def _fetch_new_listings(self) -> list[dict[str, Any]]:
        """Fetch recently listed coins."""
        try:
            data = self.client.get(
                f"{self.BASE_URL}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 50,
                    "page": 1,
                    "sparkline": "false",
                    "price_change_percentage": "1h,24h,7d",
                },
            ).json()
            items = []
            for coin in data:
                items.append({
                    "title": coin.get("name", ""),
                    "text": f"{coin.get('name', '')} ({coin.get('symbol', '')}) market data",
                    "url": f"https://www.coingecko.com/en/coins/{coin.get('id', '')}",
                    "published": utc_now(),
                    "source_domain": "coingecko.com",
                    "source_type": "market_data",
                    "metrics": {
                        "coingecko_id": coin.get("id"),
                        "symbol": coin.get("symbol"),
                        "current_price": coin.get("current_price"),
                        "market_cap": coin.get("market_cap"),
                        "total_volume": coin.get("total_volume"),
                        "price_change_1h": coin.get("price_change_percentage_1h_in_currency"),
                        "price_change_24h": coin.get("price_change_percentage_24h"),
                        "price_change_7d": coin.get("price_change_percentage_7d_in_currency"),
                        "market_cap_rank": coin.get("market_cap_rank"),
                        "circulating_supply": coin.get("circulating_supply"),
                        "total_supply": coin.get("total_supply"),
                        "ath": coin.get("ath"),
                        "ath_change_pct": coin.get("ath_change_percentage"),
                    },
                })
            return items
        except Exception:
            return []

    def _fetch_top_gainers(self) -> list[dict[str, Any]]:
        """Fetch top gainers (coins with highest 24h price increase)."""
        try:
            data = self.client.get(
                f"{self.BASE_URL}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "volume_desc",
                    "per_page": 50,
                    "page": 1,
                    "sparkline": "false",
                    "price_change_percentage": "1h,24h",
                },
            ).json()
            # Filter for high-volume coins with significant price movement
            gainers = [
                c for c in data
                if (c.get("price_change_percentage_24h") or 0) > 10
                and (c.get("total_volume") or 0) > 100000
            ][:20]
            items = []
            for coin in gainers:
                items.append({
                    "title": coin.get("name", ""),
                    "text": f"{coin.get('name', '')} up {coin.get('price_change_percentage_24h', 0):.1f}% in 24h",
                    "url": f"https://www.coingecko.com/en/coins/{coin.get('id', '')}",
                    "published": utc_now(),
                    "source_domain": "coingecko.com",
                    "source_type": "market_data",
                    "metrics": {
                        "coingecko_id": coin.get("id"),
                        "symbol": coin.get("symbol"),
                        "current_price": coin.get("current_price"),
                        "market_cap": coin.get("market_cap"),
                        "volume_24h": coin.get("total_volume"),
                        "price_change_24h_pct": coin.get("price_change_percentage_24h"),
                        "market_cap_rank": coin.get("market_cap_rank"),
                        "gainer": True,
                    },
                })
            return items
        except Exception:
            return []

    def fetch_ohlcv(self, coin_id: str, days: int = 7) -> list[dict[str, Any]]:
        """Fetch OHLC data for a specific coin (for historical analysis)."""
        try:
            data = self.client.get(
                f"{self.BASE_URL}/coins/{coin_id}/ohlc",
                params={"vs_currency": "usd", "days": days},
            ).json()
            return [
                {
                    "timestamp": candle[0],
                    "open": candle[1],
                    "high": candle[2],
                    "low": candle[3],
                    "close": candle[4],
                }
                for candle in data
            ]
        except Exception:
            return []

    def fetch_market_chart(self, coin_id: str, days: int = 30) -> dict[str, Any]:
        """Fetch market chart data (prices, volumes, market caps)."""
        try:
            return self.client.get(
                f"{self.BASE_URL}/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": days},
            ).json()
        except Exception:
            return {}
