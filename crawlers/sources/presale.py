"""Presale Monitor Night Crawler — launchpad and presale intelligence.

Tracks token presales, IDOs, IEOs, and launchpad allocations across
multiple platforms. Early presale signals often predict which tokens
will be the next hype coins before they hit DEX trading.
"""
from __future__ import annotations

from typing import Any

from common.time import utc_now
from crawlers.base import BaseCrawler


class PresaleCrawler(BaseCrawler):
    """Monitors crypto presale platforms and launchpads."""

    def __init__(self) -> None:
        super().__init__(
            name="presale",
            max_retries=2,
            retry_delay_seconds=3.0,
            rate_limit_pause=1.5,
            timeout_seconds=15.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_pinksale())
        items.extend(self._fetch_cryptorank())
        return items

    def _fetch_pinksale(self) -> list[dict[str, Any]]:
        """Fetch upcoming and active presales from PinkSale."""
        try:
            data = self.client.get(
                "https://api.pinksale.finance/api/presales",
                params={"status": "upcoming", "limit": 30, "offset": 0},
            ).json()
            items = []
            for sale in (data.get("data") or [])[:30]:
                items.append({
                    "title": sale.get("name", ""),
                    "text": f"{sale.get('name', '')} presale on {sale.get('platform', 'PinkSale')}",
                    "url": f"https://pinksale.finance/launchpad/{sale.get('address', '')}",
                    "published": utc_now(),
                    "source_domain": "pinksale.finance",
                    "source_type": "presale",
                    "metrics": {
                        "platform": "pinksale",
                        "address": sale.get("address"),
                        "token_name": sale.get("name"),
                        "token_symbol": sale.get("symbol"),
                        "chain": sale.get("chain"),
                        "soft_cap": sale.get("softCap"),
                        "hard_cap": sale.get("hardCap"),
                        "start_time": sale.get("startTime"),
                        "end_time": sale.get("endTime"),
                        "presale_status": sale.get("status"),
                    },
                })
            return items
        except Exception:
            return []

    def _fetch_cryptorank(self) -> list[dict[str, Any]]:
        """Fetch upcoming token launches from CryptoRank."""
        try:
            data = self.client.get(
                "https://api.cryptorank.io/v0/upcoming-events",
                params={"limit": 30, "type": "ido,ieo,launchpad"},
            ).json()
            items = []
            for event in (data.get("data") or [])[:30]:
                items.append({
                    "title": event.get("name", ""),
                    "text": f"{event.get('name', '')} {event.get('type', '').upper()} on "
                    f"{event.get('platform', '')}",
                    "url": f"https://cryptorank.io/event/{event.get('slug', '')}",
                    "published": utc_now(),
                    "source_domain": "cryptorank.io",
                    "source_type": "presale",
                    "metrics": {
                        "platform": event.get("platform"),
                        "event_type": event.get("type"),
                        "chain": event.get("chain"),
                        "start_date": event.get("startDate"),
                        "end_date": event.get("endDate"),
                        "token_name": event.get("name"),
                        "token_symbol": event.get("symbol"),
                        "total_raised": event.get("totalRaised"),
                    },
                })
            return items
        except Exception:
            return []
