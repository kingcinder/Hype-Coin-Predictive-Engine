"""Google Trends Night Crawler — narrative momentum from realtime searches.

Pulls Google's realtime trending searches (no API key; the unofficial
``/trends/api/realtimetrends`` endpoint) and filters for crypto-relevant
queries, so the engine sees search-level narrative momentum — the earliest
mass-attention signal, before exchange listings or price action.
"""

from __future__ import annotations

import json
from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler

log = get_logger(__name__)

TRENDS_URL = "https://trends.google.com/trends/api/realtimetrends"

# Crypto-adjacent keywords — a trending query containing one of these is
# narrative-relevant; others are dropped as off-topic noise.
_CRYPTO_KEYWORDS = (
    "crypto",
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "solana",
    "sol",
    "token",
    "coin",
    "meme",
    "memecoin",
    "airdrop",
    "presale",
    "nft",
    "defi",
    "altcoin",
    "dogecoin",
    "doge",
    "shib",
    "pepe",
    "wallet",
    "mining",
    "blockchain",
    "web3",
)


class GoogleTrendsCrawler(BaseCrawler):
    """Crawls Google Trends realtime searches for crypto narrative momentum."""

    def __init__(self, geo: str = "US", hl: str = "en-US") -> None:
        self._geo = geo
        self._hl = hl
        super().__init__(
            name="google_trends",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=2.0,
            timeout_seconds=15.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        try:
            resp = self.client.get(
                TRENDS_URL,
                params={
                    "hl": self._hl,
                    "tz": "-420",
                    "geo": self._geo,
                    "ns": "15",
                },
            )
            if resp.status_code != 200:
                log.debug("google_trends_non_200", status=resp.status_code)
                return []
            data = self._parse_jsonp(resp.text)
            if not data:
                return []
            return self._extract_crypto_trends(data)
        except Exception as exc:  # noqa: BLE001
            log.debug("google_trends_failed", error=str(exc))
            return []

    @staticmethod
    def _parse_jsonp(text: str) -> dict[str, Any] | None:
        """Strip Google's JSONP padding (``)]}',``) and parse the JSON.

        The prefix is exactly ``)]}',`` — five characters including the
        trailing comma, followed by whitespace and the JSON body.  Rather
        than hard-coding the offset, every leading padding character is
        stripped with ``lstrip`` so a variant prefix cannot leave a stray
        comma that breaks ``json.loads``.
        """
        stripped = text.strip().lstrip(")]}',\n\t ")
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def _extract_crypto_trends(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Walk the realtime-trends payload for crypto-relevant queries."""
        items: list[dict[str, Any]] = []
        try:
            realtrends = data.get("initialData", {}).get("data", {}).get("realtrends") or []
            for trend in realtrends:
                title = (trend.get("title") or {}).get("query", "")
                if not title or not any(kw in title.lower() for kw in _CRYPTO_KEYWORDS):
                    continue
                traffic = trend.get("formattedTraffic", "")
                items.append(
                    {
                        "title": f"Trending: {title}",
                        "text": (
                            f"'{title}' is trending on Google ({traffic}) — "
                            "search-level narrative momentum."
                        ),
                        "url": f"https://www.google.com/search?q={title}",
                        "published": utc_now(),
                        "source_domain": "google.com",
                        "source_type": "news",
                        "metrics": {
                            "query": title,
                            "traffic": traffic,
                            "geo": self._geo,
                            "narrative_momentum": True,
                        },
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("google_trends_extract_failed", error=str(exc))
        return items[:20]
