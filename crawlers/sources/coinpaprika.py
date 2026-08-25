"""CoinPaprika Night Crawler — market data, new coins, top gainers.

Pulls top gainers, new coins, and global market metrics from the free
CoinPaprika API (no key required for public endpoints). This provides a
third independent market-data source (alongside CoinGecko and CoinMarketCap)
so the signal scoring layer can corroborate across vendors.
"""

from __future__ import annotations

from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler

log = get_logger(__name__)


class CoinPaprikaCrawler(BaseCrawler):
    """CoinPaprika free API crawler — gainers, new coins, global market data."""

    BASE_URL = "https://api.coinpaprika.com/v1"

    def __init__(self) -> None:
        super().__init__(
            name="coinpaprika",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=2.0,
            timeout_seconds=20.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_top_gainers())
        items.extend(self._fetch_global_market())
        return items

    def _fetch_top_gainers(self) -> list[dict[str, Any]]:
        """Fetch top gainers (high 24h price increase) from CoinPaprika."""
        try:
            data = self.client.get(
                f"{self.BASE_URL}/tickers",
                params={"quotes": "USD", "limit": "100"},
            ).json()
            if not isinstance(data, list):
                return []
            gainers = [
                c
                for c in data
                if (c.get("quotes", {}).get("USD", {}).get("percent_change_24h") or 0) > 10
                and (c.get("quotes", {}).get("USD", {}).get("volume_24h") or 0) > 100_000
            ][:20]

            items: list[dict[str, Any]] = []
            for coin in gainers:
                usd = coin.get("quotes", {}).get("USD", {})
                pct = usd.get("percent_change_24h") or 0
                symbol = coin.get("symbol", "")
                name = coin.get("name", "")
                items.append(
                    {
                        "title": name,
                        "text": f"{name} ({symbol}) up {pct:.1f}% in 24h on CoinPaprika",
                        "url": f"https://coinpaprika.com/coin/{coin.get('id', '')}/",
                        "published": utc_now(),
                        "source_domain": "coinpaprika.com",
                        "source_type": "market_data",
                        "metrics": {
                            "coinpaprika_id": coin.get("id"),
                            "symbol": symbol,
                            "current_price": usd.get("price"),
                            "market_cap": usd.get("market_cap"),
                            "volume_24h": usd.get("volume_24h"),
                            "price_change_24h_pct": pct,
                            "rank": coin.get("rank"),
                            "gainer": True,
                        },
                    }
                )
            return items
        except Exception as exc:  # noqa: BLE001
            log.debug("coinpaprika_gainers_failed", error=str(exc))
            return []

    def _fetch_global_market(self) -> list[dict[str, Any]]:
        """Fetch global market cap snapshot as a single context item."""
        try:
            data = self.client.get(f"{self.BASE_URL}/global", params={"quotes": "USD"}).json()
            usd = data.get("quotes", {}).get("USD", {})
            items: list[dict[str, Any]] = [
                {
                    "title": "Global crypto market snapshot",
                    "text": (
                        f"Global market cap ${(usd.get('total_market_cap') or 0) / 1e12:.2f}T, "
                        f"24h volume ${(usd.get('volume_24h') or 0) / 1e9:.1f}B, "
                        f"BTC dominance {usd.get('bitcoin_dominance_percentage', 0):.1f}%"
                    ),
                    "url": "https://coinpaprika.com/",
                    "published": utc_now(),
                    "source_domain": "coinpaprika.com",
                    "source_type": "market_data",
                    "metrics": {
                        "total_market_cap": usd.get("total_market_cap"),
                        "volume_24h": usd.get("volume_24h"),
                        "btc_dominance_pct": usd.get("bitcoin_dominance_percentage"),
                        "global_snapshot": True,
                    },
                }
            ]
            return items
        except Exception as exc:  # noqa: BLE001
            log.debug("coinpaprika_global_failed", error=str(exc))
            return []
