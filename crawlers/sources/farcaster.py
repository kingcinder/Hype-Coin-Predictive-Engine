"""Farcaster Night Crawler — crypto project mentions and developer activity.

Uses the Farcaster public search API (hub-api.neynar.com) to crawl casts
(posts) for crypto project mentions, developer activity, and early signals.
No API key required for public search endpoints. Rate-limited to ~5 req/min
on the free tier.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler
from crawlers.sources.utils import extract_token_mentions

log = get_logger(__name__)

# Crypto-specific search queries for early project discovery
_FARCASTER_SEARCH_QUERIES = [
    "token launch",
    "new memecoin",
    "presale live",
    "dev activity",
    "smart contract deploy",
    "liquidity pool create",
    "airdrop claim",
    "NFT mint",
    "DAO proposal",
    "protocol upgrade",
    "base chain launch",
    "solana token",
]


class FarcasterCrawler(BaseCrawler):
    """Crawls Farcaster for early crypto project mentions and developer activity.

    Uses the Neynar public search API which provides free access to Farcaster
    cast search without requiring authentication.
    """

    # Neynar public API base — no API key needed for search
    NEYNAR_BASE = "https://hub-api.neynar.com/v1"

    def __init__(self, search_queries: list[str] | None = None) -> None:
        super().__init__(
            name="farcaster",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=2.0,
            timeout_seconds=15.0,
        )
        self.search_queries = search_queries or _FARCASTER_SEARCH_QUERIES

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cutoff = utc_now() - timedelta(hours=24)

        for query in self.search_queries[:6]:  # Limit queries per run
            try:
                casts = self._search_casts(query, cutoff)
                items.extend(casts)
            except Exception as exc:  # noqa: BLE001
                log.debug("farcaster_search_error", query=query, error=str(exc))
                continue

        return items

    def _search_casts(self, query: str, cutoff: datetime) -> list[dict[str, Any]]:
        """Search Farcaster casts via Neynar public search API."""
        url = f"{self.NEYNAR_BASE}/search/casts"
        try:
            response = self.client.get(
                url,
                params={
                    "q": query,
                    "limit": 25,
                    "sort_type": "recent",
                },
                headers={"Accept": "application/json"},
            )
            if response.status_code != 200:
                log.debug(
                    "farcaster_api_error",
                    status=response.status_code,
                    query=query,
                )
                return []

            data = response.json()
            return self._parse_casts(data, query, cutoff)
        except Exception as exc:  # noqa: BLE001
            log.debug("farcaster_request_error", query=query, error=str(exc))
            return []

    def _parse_casts(
        self,
        data: dict[str, Any],
        query: str,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        """Parse Farcaster cast search results into structured items."""
        items: list[dict[str, Any]] = []
        casts = data.get("result", {}).get("casts", [])

        for cast in casts:
            try:
                text = cast.get("text", "")
                if not text or len(text) < 15:
                    continue

                # Parse timestamp
                timestamp_str = cast.get("timestamp", "")
                published = _parse_farcaster_timestamp(timestamp_str)

                # Check if cast is within our time window
                if published and published < cutoff:
                    continue

                # Extract token mentions
                token_mentions = _extract_token_mentions(text)

                # Get engagement metrics
                reactions = cast.get("reactions", {})
                likes = reactions.get("likes_count", 0)
                recasts = reactions.get("recasts_count", 0)
                replies = reactions.get("replies_count", 0)

                # Get author info
                author = cast.get("author", {})
                author_username = author.get("username", "unknown")
                author_followers = author.get("follower_count", 0)

                items.append(
                    {
                        "title": text[:128],
                        "text": text[:512],
                        "url": f"https://warpcast.com/{author_username}/{cast.get('hash', '')}",
                        "published": published.isoformat() if published else utc_now().isoformat(),
                        "source_domain": "farcaster.com",
                        "source_type": "social",
                        "metrics": {
                            "search_term": query,
                            "token_mentions": token_mentions,
                            "platform": "farcaster",
                            "author_username": author_username,
                            "author_followers": author_followers,
                            "likes": likes,
                            "recasts": recasts,
                            "replies": replies,
                            "engagement_score": likes + (recasts * 2) + replies,
                            "cast_hash": cast.get("hash", ""),
                        },
                    }
                )
            except Exception:  # noqa: BLE001
                continue

        return items[:15]  # Cap per query


def _extract_token_mentions(text: str) -> list[str]:
    """Extract unique token mentions from text (delegates to shared utility)."""
    return extract_token_mentions(text)


def _parse_farcaster_timestamp(ts: str) -> datetime | None:
    """Parse Farcaster ISO timestamp string."""
    if not ts:
        return None
    try:
        # Farcaster uses ISO 8601 format
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
