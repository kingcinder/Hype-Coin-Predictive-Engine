"""X/Twitter Trends Night Crawler — which crypto topics are heating up.

Tracks mention volume per crypto keyword across the X/Twitter firehose
(via Nitter RSS — no API key required). When a keyword's fresh-tweet volume
clears a threshold, it is emitted as a ranked "trending topic" item so the
engine can see narratives gaining momentum before price data catches up.
"""

from __future__ import annotations

from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler
from crawlers.sources.nitter import NitterCrawler, parse_nitter_rss

log = get_logger(__name__)

# Emit a trend item when a keyword produced at least this many fresh tweets.
TREND_MIN_TWEETS = 3
# Max keywords scanned per run (keeps us polite to Nitter instances). With a
# keyword list longer than this, the tail is only scanned once enough earlier
# keywords rotate off the window (the orchestrator passes the full config CSV).
MAX_KEYWORDS_PER_RUN = 8


class XTrendsCrawler(BaseCrawler):
    """Crawls X/Twitter via Nitter RSS to rank crypto keyword momentum."""

    def __init__(self, keywords: list[str] | None = None) -> None:
        super().__init__(
            name="x_trends",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=2.0,
            timeout_seconds=15.0,
        )
        self.keywords = [k.strip() for k in (keywords or []) if k.strip()] or [
            "BTC",
            "crypto",
            "bitcoin",
            "ethereum",
            "solana",
            "token",
            "memecoin",
            "airdrop",
            "presale",
            "nft",
            "defi",
            "altcoin",
        ]

    def fetch_items(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for keyword in self.keywords[:MAX_KEYWORDS_PER_RUN]:
            count = self._count_tweets(keyword)
            if count > 0:
                counts[keyword] = count

        if not counts:
            return []

        # Rank by tweet volume — biggest movers first
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        items: list[dict[str, Any]] = []
        for rank, (keyword, count) in enumerate(ranked, start=1):
            if count < TREND_MIN_TWEETS:
                continue
            items.append(
                {
                    "title": f"Trending: {keyword}",
                    "text": (
                        f"'{keyword}' is trending on crypto X with {count} fresh tweets this pass."
                    ),
                    "url": f"https://x.com/search?q={keyword}&src=typed_query",
                    "published": utc_now(),
                    "source_domain": "x.com",
                    "source_type": "social",
                    "metrics": {
                        "keyword": keyword,
                        "tweet_count": count,
                        "rank": rank,
                        "platform": "twitter",
                        "trend_score": min(100.0, count * 12.0),
                    },
                }
            )
        return items

    def _count_tweets(self, keyword: str) -> int:
        """Count fresh tweets matching a keyword via Nitter RSS search."""
        for instance in NitterCrawler.NITTER_INSTANCES:
            try:
                url = f"{instance}/search/rss"
                response = self.client.get(url, params={"f": "tweets", "q": keyword})
                if response.status_code != 200:
                    continue
                return len(parse_nitter_rss(response.text, keyword))
            except Exception as exc:  # noqa: BLE001
                log.debug("xtrends_rss_error", instance=instance, keyword=keyword, error=str(exc))
                continue
        return 0
