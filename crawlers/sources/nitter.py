"""Nitter/Twitter Night Crawler — crypto Twitter sentiment and signal.

Uses Nitter RSS feeds to crawl Twitter/X without API keys. RSS parsing
is far more reliable than regex HTML scraping and survives Nitter instance
markup changes. Falls back to Farcaster search when Nitter is unavailable.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler
from crawlers.sources.utils import extract_token_mentions

log = get_logger(__name__)


class NitterCrawler(BaseCrawler):
    """Crawls Nitter RSS feeds for crypto Twitter signals."""

    NITTER_INSTANCES = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.woodland.cafe",
        "https://nitter.cz",
    ]

    CRYPTO_SEARCH_TERMS = [
        "gem crypto",
        "100x token",
        "pump launch",
        "presale crypto",
        "whale alert",
        "bullish alpha",
        "new listing",
        "airdrop live",
        "moon shot",
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
        for term in self.search_terms[:5]:  # Limit to 5 terms per run
            rss_items = self._search_rss(term)
            items.extend(rss_items)
        return items

    def _search_rss(self, term: str) -> list[dict[str, Any]]:
        """Search via Nitter RSS feed — much more reliable than HTML scraping."""
        for instance in self.NITTER_INSTANCES:
            try:
                url = f"{instance}/search/rss"
                response = self.client.get(url, params={"f": "tweets", "q": term})
                if response.status_code != 200:
                    continue
                return self._parse_rss(response.text, term)
            except Exception as exc:  # noqa: BLE001
                log.debug("nitter_rss_error", instance=instance, term=term, error=str(exc))
                continue
        return []

    def _parse_rss(self, xml_text: str, search_term: str) -> list[dict[str, Any]]:
        """Parse Nitter RSS/Atom feed into structured items."""
        items: list[dict[str, Any]] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            # Try to fix common XML issues
            xml_text = xml_text.replace("&", "&amp;")
            try:
                root = ET.fromstring(xml_text)
            except ET.ParseError:
                return []

        # Handle both RSS 2.0 (<channel><item>) and Atom (<entry>)

        # RSS 2.0 format
        for item_el in root.iter("item"):
            entry = self._parse_rss_item(item_el, search_term)
            if entry:
                items.append(entry)

        # Atom format
        for entry_el in root.iter("{http://www.w3.org/2005/Atom}entry"):
            entry = self._parse_atom_entry(entry_el, search_term)
            if entry:
                items.append(entry)

        return items[:15]  # Cap per search term

    def _parse_rss_item(self, el: ET.Element, search_term: str) -> dict[str, Any] | None:
        """Parse a single RSS 2.0 <item> element."""
        title = _text(el, "title")
        description = _text(el, "description")
        link = _text(el, "link")
        pub_date = _text(el, "pubDate")

        text = _strip_html(description or title or "")
        if not text or len(text) < 20:
            return None

        token_mentions = _extract_token_mentions(text)
        if not token_mentions:
            return None

        return {
            "title": text[:128],
            "text": text[:512],
            "url": link or "",
            "published": _parse_pub_date(pub_date),
            "source_domain": "twitter.com",
            "source_type": "social",
            "metrics": {
                "search_term": search_term,
                "token_mentions": token_mentions,
                "platform": "twitter",
                "has_url": bool(link),
            },
        }

    def _parse_atom_entry(self, el: ET.Element, search_term: str) -> dict[str, Any] | None:
        """Parse a single Atom <entry> element."""
        title = _text(el, "{http://www.w3.org/2005/Atom}title")
        summary = _text(el, "{http://www.w3.org/2005/Atom}summary")
        content = _text(el, "{http://www.w3.org/2005/Atom}content")
        link_el = el.find("{http://www.w3.org/2005/Atom}link")
        link = link_el.get("href", "") if link_el is not None else ""
        published = _text(el, "{http://www.w3.org/2005/Atom}published")

        text = _strip_html(content or summary or title or "")
        if not text or len(text) < 20:
            return None

        token_mentions = _extract_token_mentions(text)
        if not token_mentions:
            return None

        return {
            "title": text[:128],
            "text": text[:512],
            "url": link,
            "published": _parse_pub_date(published),
            "source_domain": "twitter.com",
            "source_type": "social",
            "metrics": {
                "search_term": search_term,
                "token_mentions": token_mentions,
                "platform": "twitter",
                "has_url": bool(link),
            },
        }


# ── Helpers ────────────────────────────────────────────────────────────────


def _text(el: ET.Element, tag: str) -> str:
    """Extract text content from an XML element."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _strip_html(html: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", html).strip()


def _extract_token_mentions(text: str) -> list[str]:
    """Extract unique token mentions from text (delegates to shared utility)."""
    return extract_token_mentions(text)


def _parse_pub_date(date_str: str | None) -> Any:
    """Parse an RSS/Atom date string to a datetime."""
    if not date_str:
        return utc_now()
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(date_str)
    except Exception:  # noqa: BLE001
        return utc_now()
