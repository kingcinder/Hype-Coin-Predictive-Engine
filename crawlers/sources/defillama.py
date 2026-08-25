"""DeFiLlama Night Crawler — TVL tracking, protocol launches, yield data.

DeFiLlama is the authoritative source for DeFi TVL data. This crawler
tracks TVL changes, new protocol launches, and yield opportunities that
signal emerging hype coins in the DeFi space.
"""

from __future__ import annotations

from typing import Any

from common.time import utc_now
from crawlers.base import BaseCrawler


class DeFiLlamaCrawler(BaseCrawler):
    """Crawls DeFiLlama for TVL data, new protocols, and yield info."""

    BASE_URL = "https://api.llama.fi"

    def __init__(self) -> None:
        super().__init__(
            name="defillama",
            max_retries=2,
            retry_delay_seconds=3.0,
            rate_limit_pause=1.0,
            timeout_seconds=15.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_new_protocols())
        items.extend(self._fetch_tvl_gainers())
        items.extend(self._fetch_yields())
        return items

    def _fetch_new_protocols(self) -> list[dict[str, Any]]:
        """Fetch recently added protocols."""
        try:
            data = self.client.get(f"{self.BASE_URL}/protocols").json()
            items = []
            # Sort by change_1d descending to find rapidly growing protocols
            protocols = sorted(
                [p for p in data if isinstance(p, dict)],
                key=lambda p: float(p.get("change_1d") or 0),
                reverse=True,
            )[:30]
            for proto in protocols:
                change_1d = float(proto.get("change_1d") or 0)
                if abs(change_1d) < 5:
                    continue  # Skip low-change protocols
                items.append(
                    {
                        "title": proto.get("name", ""),
                        "text": f"{proto.get('name', '')} TVL changed {change_1d:.1f}% in 24h",
                        "url": proto.get(
                            "url", f"https://defillama.com/protocol/{proto.get('slug', '')}"
                        ),
                        "published": utc_now(),
                        "source_domain": "defillama.com",
                        "source_type": "defi_data",
                        "metrics": {
                            "slug": proto.get("slug"),
                            "tvl": proto.get("tvl"),
                            "change_1d": change_1d,
                            "change_7d": float(proto.get("change_7d") or 0),
                            "chains": proto.get("chains", []),
                            "category": proto.get("category"),
                            "chain": proto.get("chain"),
                            "mcap": proto.get("mcap"),
                        },
                    }
                )
            return items
        except Exception:
            return []

    def _fetch_tvl_gainers(self) -> list[dict[str, Any]]:
        """Fetch protocols with highest TVL growth."""
        try:
            data = self.client.get(f"{self.BASE_URL}/tvl/gainers").json()
            items = []
            for proto in (data if isinstance(data, list) else [])[:20]:
                items.append(
                    {
                        "title": proto.get("name", ""),
                        "text": f"{proto.get('name', '')} TVL gainer: "
                        f"{proto.get('change_1d', 0):.1f}% daily",
                        "url": f"https://defillama.com/protocol/{proto.get('slug', '')}",
                        "published": utc_now(),
                        "source_domain": "defillama.com",
                        "source_type": "defi_data",
                        "metrics": {
                            "slug": proto.get("slug"),
                            "tvl": proto.get("tvl"),
                            "change_1d": proto.get("change_1d"),
                            "category": proto.get("category"),
                            "tvl_gainer": True,
                        },
                    }
                )
            return items
        except Exception:
            return []

    def _fetch_yields(self) -> list[dict[str, Any]]:
        """Fetch highest yield pools (potential hype signals)."""
        try:
            data = self.client.get(f"{self.BASE_URL}/pools").json()
            pools = (data.get("data") or []) if isinstance(data, dict) else []
            # Filter for high-APY pools with meaningful TVL
            high_yield = [
                p
                for p in pools
                if float(p.get("apy") or 0) > 50 and float(p.get("tvlUsd") or 0) > 100000
            ][:20]
            items = []
            for pool in high_yield:
                items.append(
                    {
                        "title": pool.get("project", ""),
                        "text": f"{pool.get('project', '')} {pool.get('symbol', '')} yields "
                        f"{float(pool.get('apy', 0)):.1f}% APY",
                        "url": f"https://defillama.com/yields/pool/{pool.get('pool', '')}",
                        "published": utc_now(),
                        "source_domain": "defillama.com",
                        "source_type": "yield_data",
                        "metrics": {
                            "pool": pool.get("pool"),
                            "project": pool.get("project"),
                            "symbol": pool.get("symbol"),
                            "chain": pool.get("chain"),
                            "apy": float(pool.get("apy") or 0),
                            "tvl_usd": float(pool.get("tvlUsd") or 0),
                            "apy_base": pool.get("apyBase"),
                            "apy_reward": pool.get("apyReward"),
                            "stablecoin": pool.get("stablecoin", False),
                        },
                    }
                )
            return items
        except Exception:
            return []
