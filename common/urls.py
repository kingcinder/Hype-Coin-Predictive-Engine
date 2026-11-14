"""Redact credentials embedded in URLs before persisting, returning or alerting."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Providers commonly pass credentials as query parameters (Helius, QuickNode,
# Alchemy). They must never reach the database, the API or a notification.
_SENSITIVE_QUERY_KEYS = frozenset(
    {"api-key", "api_key", "apikey", "key", "token", "access_token", "secret"}
)


def redact_url(url: str) -> str:
    """Return ``url`` with sensitive query values replaced by ``***``."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    query = [
        (key, "***" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, safe="*"), parts.fragment)
    )
