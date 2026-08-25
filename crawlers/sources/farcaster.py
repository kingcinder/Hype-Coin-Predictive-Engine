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

    Uses the Neynar public search API. Without an API key, rate-limited to
    ~5 req/min on the free tier. With a valid ``farcaster_api_key``, requests
    are sent with an ``Authorization: Bearer`` header for higher limits.
    """

    # Neynar public API base
    NEYNAR_BASE = "https://hub-api.neynar.com/v1"

    def __init__(
        self,
        search_queries: list[str] | None = None,
        api_key: str | None = None,
        tracked_fids: list[str] | None = None,
        tracked_channels: list[str] | None = None,
    ) -> None:
        super().__init__(
            name="farcaster",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=2.0,
            timeout_seconds=15.0,
        )
        self.search_queries = search_queries or _FARCASTER_SEARCH_QUERIES
        self._api_key = api_key
        self._tracked_fids: list[str] = tracked_fids or []
        self._tracked_channels: list[str] = tracked_channels or []

    def _build_headers(self) -> dict[str, str]:
        """Build request headers, including Authorization if an API key is set."""
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cutoff = utc_now() - timedelta(hours=24)
        seen_hashes: set[str] = set()

        def _add_unique(casts: list[dict[str, Any]]) -> None:
            """Append casts that haven't been seen yet (dedup by cast_hash)."""
            for cast in casts:
                h = cast.get("metrics", {}).get("cast_hash", "")
                if h and h in seen_hashes:
                    continue
                if h:
                    seen_hashes.add(h)
                items.append(cast)

        # 1. Keyword search queries
        for query in self.search_queries[:6]:
            try:
                _add_unique(self._search_casts(query, cutoff))
            except Exception as exc:  # noqa: BLE001
                log.debug("farcaster_search_error", query=query, error=str(exc))
                continue

        # 2. Tracked developer FIDs
        for fid in self._tracked_fids[:10]:
            try:
                _add_unique(self._fetch_user_casts(fid, cutoff))
            except Exception as exc:  # noqa: BLE001
                log.debug("farcaster_fid_error", fid=fid, error=str(exc))
                continue

        # 3. Tracked channels for trending discussions
        for channel in self._tracked_channels[:8]:
            try:
                _add_unique(self._fetch_channel_casts(channel, cutoff))
            except Exception as exc:  # noqa: BLE001
                log.debug("farcaster_channel_error", channel=channel, error=str(exc))
                continue

        return items

    def _fetch_user_casts(self, fid: str, cutoff: datetime) -> list[dict[str, Any]]:
        """Fetch recent casts from a specific Farcaster user by FID.

        Uses the ``/v1/user/casts`` endpoint. The response structure mirrors
        ``/v1/search/casts`` so we reuse ``_parse_casts`` with a synthetic
        query label.
        """
        url = f"{self.NEYNAR_BASE}/user/casts"
        try:
            response = self.client.get(
                url,
                params={"fid": fid, "limit": 25},
                headers=self._build_headers(),
            )
            if response.status_code != 200:
                log.debug(
                    "farcaster_fid_api_error",
                    status=response.status_code,
                    fid=fid,
                )
                return []

            data = response.json()
            # Reuse the cast parser with a label so we know it came from FID tracking
            return self._parse_casts(data, f"fid:{fid}", cutoff)
        except Exception as exc:  # noqa: BLE001
            log.debug("farcaster_fid_request_error", fid=fid, error=str(exc))
            return []

    def _fetch_channel_casts(self, channel: str, cutoff: datetime) -> list[dict[str, Any]]:
        """Fetch recent casts from a specific Farcaster channel by slug.

        Uses the ``/v1/channel/casts`` endpoint. The response structure mirrors
        ``/v1/search/casts`` so we reuse ``_parse_casts`` with a synthetic
        query label.
        """
        url = f"{self.NEYNAR_BASE}/channel/casts"
        try:
            response = self.client.get(
                url,
                params={"channel_id": channel, "limit": 25},
                headers=self._build_headers(),
            )
            if response.status_code != 200:
                log.debug(
                    "farcaster_channel_api_error",
                    status=response.status_code,
                    channel=channel,
                )
                return []

            data = response.json()
            return self._parse_casts(data, f"channel:{channel}", cutoff)
        except Exception as exc:  # noqa: BLE001
            log.debug("farcaster_channel_request_error", channel=channel, error=str(exc))
            return []

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
                headers=self._build_headers(),
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
        # Handle both {"result":{"casts":[...]}} and {"casts":[...]} shapes
        casts = data.get("result", {}).get("casts") or data.get("casts", [])

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

                # Compute engagement velocity metrics
                age_minutes = _age_minutes(published)
                likes_per_min = likes / max(age_minutes, 1.0)
                recast_ratio = recasts / max(likes + recasts, 1)
                # Viral velocity: weighted combo normalized to ~0-100 scale
                # High likes/min + high recast ratio = strong viral signal
                velocity_score = min(
                    100,
                    round(
                        likes_per_min * 10  # fast likes
                        + recast_ratio * 30  # amplification
                        + replies * 0.5,  # discussion depth
                        1,
                    ),
                )

                metrics: dict[str, Any] = {
                    "search_term": query,
                    "token_mentions": token_mentions,
                    "platform": "farcaster",
                    "author_username": author_username,
                    "author_followers": author_followers,
                    "likes": likes,
                    "recasts": recasts,
                    "replies": replies,
                    "engagement_score": likes + (recasts * 2) + replies,
                    "likes_per_min": round(likes_per_min, 3),
                    "recast_ratio": round(recast_ratio, 3),
                    "velocity_score": velocity_score,
                    "age_minutes": round(age_minutes, 1),
                    "cast_hash": cast.get("hash", ""),
                }
                # Include author FID for FID-tracked items (query starts with "fid:")
                if query.startswith("fid:"):
                    metrics["author_fid"] = author.get("fid")
                # Include channel slug for channel-tracked items
                if query.startswith("channel:"):
                    metrics["channel"] = query[8:]  # strip "channel:" prefix

                items.append(
                    {
                        "title": text[:128],
                        "text": text[:512],
                        "url": f"https://warpcast.com/{author_username}/{cast.get('hash', '')}",
                        "published": published.isoformat() if published else utc_now().isoformat(),
                        "source_domain": "farcaster.com",
                        "source_type": "social",
                        "metrics": metrics,
                    }
                )
            except Exception:  # noqa: BLE001
                continue

        return items[:15]  # Cap per query


def _age_minutes(published: datetime | None) -> float:
    """Return the age of a cast in minutes since publication."""
    if published is None:
        return 60.0  # assume 1 hour if timestamp missing
    delta = utc_now() - published
    return max(delta.total_seconds() / 60.0, 0.1)


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
