"""DexScreener Trends Night Crawler — paid-boosted and newly-profiled tokens.

Pulls the two free no-key DexScreener endpoints:
- ``/token-profiles/latest/v1`` — tokens that just created a profile page
  (a deliberate marketing push, often ahead of a launch).
- ``/token-boosts/top/v1`` — tokens with the most active paid boosts
  (real money betting on attention; a strong early-demand proxy).

Both are richer social-engineering signals than raw price data: a profile +
boost combination on a token nobody knows yet is exactly the kind of setup
the engine should flag early.
"""

from __future__ import annotations

from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler

log = get_logger(__name__)


class DexScreenerTrendsCrawler(BaseCrawler):
    """Crawls DexScreener token profiles and top boosts."""

    BASE_URL = "https://api.dexscreener.com"

    def __init__(self) -> None:
        super().__init__(
            name="dexscreener_trends",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=1.5,
            timeout_seconds=15.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_token_profiles())
        items.extend(self._fetch_top_boosts())
        return items

    def _fetch_token_profiles(self) -> list[dict[str, Any]]:
        """Newly-created token profile pages."""
        try:
            data = self.client.get(f"{self.BASE_URL}/token-profiles/latest/v1").json()
            if not isinstance(data, list):
                return []
            items: list[dict[str, Any]] = []
            for profile in data[:30]:
                chain = profile.get("chainId", "")
                address = profile.get("tokenAddress", "")
                if not chain or not address:
                    continue
                items.append(
                    {
                        "title": profile.get("description") or f"New profile on {chain}",
                        "text": (profile.get("description") or "")[:300],
                        "url": profile.get("url") or f"https://dexscreener.com/{chain}/{address}",
                        "published": utc_now(),
                        "source_domain": "dexscreener.com",
                        "source_type": "market_data",
                        "metrics": {
                            "chain": chain,
                            "token_address": address,
                            "profile_links": [
                                link for link in (profile.get("links") or []) if link
                            ][:6],
                            "new_profile": True,
                            "boosted": False,
                        },
                    }
                )
            return items
        except Exception as exc:  # noqa: BLE001
            log.debug("dexscreener_profiles_failed", error=str(exc))
            return []

    def _fetch_top_boosts(self) -> list[dict[str, Any]]:
        """Tokens with the most active paid boosts."""
        try:
            data = self.client.get(f"{self.BASE_URL}/token-boosts/top/v1").json()
            if not isinstance(data, list):
                return []
            items: list[dict[str, Any]] = []
            for boost in data[:30]:
                chain = boost.get("chainId", "")
                address = boost.get("tokenAddress", "")
                if not chain or not address:
                    continue
                items.append(
                    {
                        "title": f"Boosted: {address[:10]}… on {chain}",
                        "text": (
                            f"Token on {chain} with {boost.get('totalAmount', 0)} "
                            "active DexScreener boosts — real money paying for attention."
                        ),
                        "url": boost.get("url") or f"https://dexscreener.com/{chain}/{address}",
                        "published": utc_now(),
                        "source_domain": "dexscreener.com",
                        "source_type": "market_data",
                        "metrics": {
                            "chain": chain,
                            "token_address": address,
                            "boost_amount": boost.get("amount"),
                            "boost_total": boost.get("totalAmount"),
                            "new_profile": False,
                            "boosted": True,
                        },
                    }
                )
            return items
        except Exception as exc:  # noqa: BLE001
            log.debug("dexscreener_boosts_failed", error=str(exc))
            return []
