"""Redact credentials embedded in URLs before persisting, returning or alerting."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Providers commonly pass credentials as query parameters (Helius, QuickNode)
# or as a token after the API version in the path (Alchemy ``/v2/<key>``).
# Neither may reach the database, the API or a notification.
_SENSITIVE_QUERY_KEYS = frozenset(
    {"api-key", "api_key", "apikey", "key", "token", "access_token", "secret"}
)
_PATH_TOKEN = re.compile(r"(/(?:v\d+)/)([A-Za-z0-9_-]{12,})")


def redact_url(url: str) -> str:
    """Return ``url`` with sensitive query values replaced by ``***``."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    path = _PATH_TOKEN.sub(r"\1***", parts.path)
    if not parts.query:
        return urlunsplit((parts.scheme, parts.netloc, path, "", parts.fragment))
    query = [
        (key, "***" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, path, urlencode(query, safe="*"), parts.fragment)
    )
