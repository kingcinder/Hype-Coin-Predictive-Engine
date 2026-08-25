"""Shared utilities for Night Crawler data sources.

Common patterns for token mention extraction and text processing used
across multiple crawlers (nitter, farcaster, etc.).
"""

from __future__ import annotations

import re

# Token mention patterns: $TICKER, 0x addresses, Solana base58 addresses
TOKEN_PATTERN = re.compile(
    r"\$([A-Z]{2,10})\b|"
    r"(0x[a-fA-F0-9]{40})|"
    r"([1-9A-HJ-NP-Za-km-z]{32,44})"
)


def extract_token_mentions(text: str) -> list[str]:
    """Extract unique token mentions from text."""
    mentions: list[str] = []
    seen: set[str] = set()
    for match in TOKEN_PATTERN.finditer(text):
        for group in match.groups():
            if group and group not in seen:
                seen.add(group)
                mentions.append(group)
    return mentions[:5]
