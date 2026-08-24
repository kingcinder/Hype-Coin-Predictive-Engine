"""Pump.fun Night Crawler — new Solana token launches.

Pump.fun is the dominant Solana token launch platform. This crawler
discovers new tokens the moment they appear, providing early signals
that feed the ignition radar and prelaunch queue.
"""
from __future__ import annotations

from typing import Any

from common.time import utc_now
from crawlers.base import BaseCrawler


class PumpFunCrawler(BaseCrawler):
    """Crawls Pump.fun for new Solana token launches."""

    def __init__(self) -> None:
        super().__init__(
            name="pump_fun",
            max_retries=2,
            retry_delay_seconds=3.0,
            rate_limit_pause=1.0,
            timeout_seconds=15.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_latest_tokens())
        items.extend(self._fetch_graduating_tokens())
        return items

    def _fetch_latest_tokens(self) -> list[dict[str, Any]]:
        """Fetch latest tokens created on Pump.fun."""
        try:
            data = self.client.get(
                "https://frontend-api-v3.pump.fun/coins/latest",
                params={"limit": 50, "offset": 0, "includeNsfw": "false"},
            ).json()
            items = []
            tokens = data if isinstance(data, list) else data.get("coins", [])
            for token in tokens[:50]:
                items.append({
                    "title": token.get("name", ""),
                    "text": f"{token.get('name', '')} ({token.get('symbol', '')}) launched on Pump.fun",
                    "url": f"https://pump.fun/coin/{token.get('mint', '')}",
                    "published": utc_now(),
                    "source_domain": "pump.fun",
                    "source_type": "launch_platform",
                    "metrics": {
                        "mint": token.get("mint"),
                        "symbol": token.get("symbol"),
                        "name": token.get("name"),
                        "initial_buy": token.get("initial_buy"),
                        "market_cap": token.get("market_cap"),
                        "reply_count": token.get("reply_count", 0),
                        "usd_market_cap": token.get("usd_market_cap"),
                        "created_timestamp": token.get("created_timestamp"),
                        "virtual_sol_reserves": token.get("virtual_sol_reserves"),
                        "platform": "pump_fun",
                    },
                })
            return items
        except Exception:
            return []

    def _fetch_graduating_tokens(self) -> list[dict[str, Any]]:
        """Fetch tokens approaching graduation (bonding curve completion)."""
        try:
            data = self.client.get(
                "https://frontend-api-v3.pump.fun/coins/graduating",
                params={"limit": 30, "offset": 0},
            ).json()
            items = []
            tokens = data if isinstance(data, list) else data.get("coins", [])
            for token in tokens[:30]:
                items.append({
                    "title": token.get("name", ""),
                    "text": f"{token.get('name', '')} ({token.get('symbol', '')}) approaching graduation on Pump.fun",
                    "url": f"https://pump.fun/coin/{token.get('mint', '')}",
                    "published": utc_now(),
                    "source_domain": "pump.fun",
                    "source_type": "launch_platform",
                    "metrics": {
                        "mint": token.get("mint"),
                        "symbol": token.get("symbol"),
                        "name": token.get("name"),
                        "market_cap": token.get("market_cap"),
                        "usd_market_cap": token.get("usd_market_cap"),
                        "virtual_sol_reserves": token.get("virtual_sol_reserves"),
                        "graduating": True,
                        "platform": "pump_fun",
                    },
                })
            return items
        except Exception:
            return []
