"""Redact credentials embedded in URLs before persisting, returning or alerting."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Providers commonly pass credentials as query parameters (Helius, QuickNode)
# or as a token after the API version in the path (Alchemy ``/v2/<key>``).
# Neither may reach the database, the API or a notification.
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "api-key",
        "api_key",
        "apikey",
        "key",
        "token",
        "access_token",
        "access-token",
        "secret",
        "signature",
        "private_key",
    }
)
_PATH_TOKEN = re.compile(r"(/(?:v\d+)/)([A-Za-z0-9_-]{12,})")
# Chat/webhook endpoints authenticate with a token in the path, so an httpx
# error message can leak the credential without any query string.
_TELEGRAM_BOT_TOKEN = re.compile(r"(/bot)[A-Za-z0-9:_-]{20,}")
_DISCORD_WEBHOOK_TOKEN = re.compile(r"(/api/webhooks/\d+/)[A-Za-z0-9_-]{20,}")
_SLACK_WEBHOOK_TOKEN = re.compile(r"(/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/)[A-Za-z0-9_-]+")
_QUICKNODE_PATH_TOKEN = re.compile(r"^/[A-Za-z0-9_-]{12,}")
# Exception messages embed the full request URL, so a hostname-gated check on
# ``urlsplit`` is not enough: the entire message becomes the path.  Match the
# QuickNode URL directly wherever it appears in the text.
_QUICKNODE_URL_TOKEN = re.compile(
    r"(https?://[A-Za-z0-9.-]+\.quiknode\.pro/)([A-Za-z0-9_-]{12,})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


def redact_url(url: str) -> str:
    """Return ``url`` with sensitive query values replaced by ``***``."""
    if not url:
        return url
    url = _QUICKNODE_URL_TOKEN.sub(r"\1***", url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    path = _PATH_TOKEN.sub(r"\1***", parts.path)
    path = _TELEGRAM_BOT_TOKEN.sub(r"\1***", path)
    path = _DISCORD_WEBHOOK_TOKEN.sub(r"\1***", path)
    path = _SLACK_WEBHOOK_TOKEN.sub(r"\1***", path)
    if parts.hostname and parts.hostname.endswith(".quiknode.pro"):
        path = _QUICKNODE_PATH_TOKEN.sub("/***", path)
    if not parts.query:
        return urlunsplit((parts.scheme, parts.netloc, path, "", parts.fragment))
    query = [
        (key, "***" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, path, urlencode(query, safe="*"), parts.fragment)
    )
