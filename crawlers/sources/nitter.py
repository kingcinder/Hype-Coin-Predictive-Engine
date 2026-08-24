"""Nitter/Twitter Night Crawler — crypto Twitter sentiment and signal.

Uses Nitter public instances to crawl Twitter/X without API keys.
Crypto Twitter is the #1 hype signal source — this crawler detects
trending tokens, KOL mentions, and sentiment spikes.
"""
from __future__ import annotations

import re
from typing import Any

from common.time import utc_now
from crawlers.base import BaseCrawler


class NitterCrawler(BaseCrawler):
    """Crawls Nitter instances for crypto Twitter signals."""

    NITTER_INSTANCES = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.woodland.cafe",
    ]

    CRYPTO_SEARCH_TERMS = [
        "gem", "moon", "100x", "pump", "launch", "presale",
        "airdrop", "whale", "10x", "bullish", "alpha",
    ]

    def __init__(self, search_terms: list[str] | None = None) -> None:
        super().__init__(
            name="nitter",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=2.0,
            timeout_seconds=15.0,
        )
        self.search_terms = search_terms or self.CRYPTO_SEARCH_TERMS

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for term in self.search_terms:
            items.extend(self._search(term))
        return items

    def _search(self, term: str) -> list[dict[str, Any]]:
        """Search Nitter for crypto-related tweets."""
        for instance in self.NITTER_INSTANCES:
            try:
                response = self.client.get(
                    f"{instance}/search",
                    params={"f": "tweets", "q": term},
                )
                if response.status_code != 200:
                    continue
                return self._parse_search(response.text, term)
            except Exception:
                continue
        return []

    def _parse_search(self, html: str, search_term: str) -> list[dict[str, Any]]:
        """Parse Nitter search results HTML."""
        items = []
        # Simple regex extraction for tweet content
        tweet_pattern = re.compile(
            r'class="tweet-content[^"]*"[^>]*>(.*?)</div>',
            re.DOTALL,
        )
        for match in tweet_pattern.finditer(html):
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if not text or len(text) < 20:
                continue
            # Check for token mentions (addresses or symbols)
            token_mentions = re.findall(
                r'\$([A-Z]{2,10})\b|'
                r'(0x[a-fA-F0-9]{40})|'
                r'([A-Za-z0-9]{32,44})',
                text,
            )
            if not token_mentions:
                continue
            items.append({
                "title": text[:128],
                "text": text[:512],
                "url": "",
                "published": utc_now(),
                "source_domain": "twitter.com",
                "source_type": "social",
                "metrics": {
                    "search_term": search_term,
                    "token_mentions": [m for group in token_mentions for m in group if m],
                    "platform": "twitter",
                },
            })
        return items[:10]
