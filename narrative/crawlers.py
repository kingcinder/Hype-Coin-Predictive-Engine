from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import feedparser

from common.http import HttpClient
from common.logging import get_logger

log = get_logger(__name__)

try:  # Telethon is an optional extra; the engine must run without it.
    from telethon import TelegramClient

    _TELEGRAM_AVAILABLE = True
    _TelegramClient = TelegramClient
except ImportError:  # pragma: no cover - exercised when the extra is not installed.
    _TELEGRAM_AVAILABLE = False
    _TelegramClient = Any  # type: ignore[assignment]


def _iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _rss_published(entry: Any) -> datetime | None:
    """Parse an RSS entry's publish time.

    feedparser exposes ``published_parsed`` (struct_time) and ``published``
    (an RFC-822 string like "Fri, 20 Nov 2026 22:51:14 +0000"). ``_iso``
    only handles ISO-8601, so every feed date came back None and all
    news_items had a NULL published_at. Prefer the parsed struct, then
    RFC-822, then ISO.
    """
    parsed = getattr(entry, "published_parsed", None)
    if parsed is not None:
        try:
            return datetime(*parsed[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            pass
    raw = getattr(entry, "published", None)
    if raw:
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(str(raw))
            return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            pass
    return _iso(raw)


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _epoch(value: Any) -> datetime | None:
    """Reddit-style Unix-epoch-seconds timestamp -> aware UTC datetime (H14)."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class RedditCrawler:
    """Reddit posts via app-only OAuth, falling back to public JSON.

    Reddit blocks logged-out access to its public ``.json`` endpoints
    (403), so when ``REDDIT_CLIENT_ID`` / ``REDDIT_CLIENT_SECRET`` are set
    the crawler exchanges them for an app-only bearer token and reads
    ``https://oauth.reddit.com``. Without keys it keeps the old public
    path as a best-effort fallback.
    """

    _OAUTH_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    _OAUTH_BASE_URL = "https://oauth.reddit.com"

    def __init__(
        self,
        subreddits: list[str],
        limit: int = 25,
        *,
        base_url: str = "https://www.reddit.com",
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self.subreddits = subreddits
        self.limit = limit
        self.base_url = base_url.rstrip("/")
        self.http = HttpClient(base_url=self.base_url)
        self.client_id = client_id
        self.client_secret = client_secret
        self._oauth_http: HttpClient | None = None
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def close(self) -> None:
        self.http.close()
        if self._oauth_http is not None:
            self._oauth_http.close()

    def _oauth_token(self) -> str | None:
        """App-only OAuth token, cached until shortly before expiry."""
        if not self.client_id or not self.client_secret:
            return None
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        try:
            import httpx

            resp = httpx.post(
                self._OAUTH_TOKEN_URL,
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": "serpent-hype-coin-engine/0.1"},
                timeout=10.0,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - no token, fall back to public
            log.debug("reddit_oauth_token_failed", error=str(exc))
            return None
        token = payload.get("access_token")
        if not token:
            return None
        expires_in = payload.get("expires_in") or 3600
        self._token = token
        self._token_expires_at = time.monotonic() + max(60, int(expires_in) - 60)
        return token

    def _http_and_headers(self) -> tuple[HttpClient, dict[str, str] | None, str | None]:
        token = self._oauth_token()
        if token:
            if self._oauth_http is None:
                self._oauth_http = HttpClient(base_url=self._OAUTH_BASE_URL)
            return self._oauth_http, {"Authorization": f"Bearer {token}"}, token
        return self.http, None, None

    def _fetch_subreddit(
        self, http: HttpClient, subreddit: str, headers: dict[str, str] | None
    ) -> Any:
        return http.get_json(
            f"/r/{subreddit}/new.json", params={"limit": self.limit}, headers=headers
        )

    def fetch(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        http, headers, token = self._http_and_headers()
        for subreddit in self.subreddits:
            try:
                data = self._fetch_subreddit(http, subreddit, headers)
            except Exception as exc:  # noqa: BLE001 - 401 gets one token refresh
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if token and status == 401:
                    # Token went stale mid-cycle; refresh once and retry.
                    self._token = None
                    self._token_expires_at = 0.0
                    http, headers, token = self._http_and_headers()
                    data = self._fetch_subreddit(http, subreddit, headers)
                else:
                    raise
            for child in (data.get("data") or {}).get("children") or []:
                post = child.get("data") or {}
                title = str(post.get("title") or "").strip()
                if not title:
                    continue
                output.append(
                    {
                        "title": title,
                        "text": f"{title} {post.get('selftext') or ''}",
                        "url": f"{self.base_url}{post.get('permalink') or ''}",
                        # H14: use the post's real creation time, not crawl
                        # time — otherwise stale posts inflate narrative
                        # velocity as breaking news when first seen.
                        "published": _epoch(post.get("created_utc")),
                        "author": str(post.get("author") or ""),
                        "source_domain": "reddit.com",
                        "metrics": {
                            "subreddit": subreddit,
                            "ups": post.get("ups", 0),
                            "num_comments": post.get("num_comments", 0),
                        },
                    }
                )
        return output


class YouTubeCrawler:
    """YouTube channel RSS feeds (free, no key)."""

    def __init__(self, channel_ids: list[str]) -> None:
        self.channel_ids = channel_ids
        self.http = HttpClient()

    def close(self) -> None:
        self.http.close()

    def fetch(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for channel_id in self.channel_ids:
            response = self.http._client.get(
                f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            )
            response.raise_for_status()
            feed = feedparser.parse(response.text)
            for entry in feed.entries:
                title = str(getattr(entry, "title", "") or "").strip()
                if not title:
                    continue
                output.append(
                    {
                        "title": title,
                        "text": title,
                        "url": str(getattr(entry, "link", "") or ""),
                        "published": _rss_published(entry),
                        "author": str(getattr(entry, "author", "") or ""),
                        "source_domain": "youtube.com",
                        "metrics": {"channel_id": channel_id},
                    }
                )
        return output


class GitHubCrawler:
    """GitHub search API (unauthenticated free tier: ~60 req/hr — enough)."""

    def __init__(
        self,
        queries: list[str],
        *,
        base_url: str = "https://api.github.com",
    ) -> None:
        self.queries = queries
        self.base_url = base_url.rstrip("/")
        self.http = HttpClient(base_url=self.base_url)

    def close(self) -> None:
        self.http.close()

    def fetch(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for query in self.queries:
            data = self.http.get_json(
                "/search/repositories",
                params={"q": query, "sort": "updated", "per_page": 10},
            )
            for item in (data.get("items") or []) if isinstance(data, dict) else []:
                full_name = str(item.get("full_name") or "").strip()
                if not full_name:
                    continue
                description = str(item.get("description") or "")
                output.append(
                    {
                        "title": full_name,
                        "text": f"{full_name} {description}".strip(),
                        "url": str(item.get("html_url") or ""),
                        "published": _iso(item.get("pushed_at")),
                        "author": str((item.get("owner") or {}).get("login") or ""),
                        "source_domain": "github.com",
                        "metrics": {
                            "stars": item.get("stargazers_count", 0),
                            "forks": item.get("forks_count", 0),
                            "language": item.get("language"),
                        },
                    }
                )
        return output


class HuggingFaceCrawler:
    """HuggingFace trending API (free). AI-narrative tokens almost always have an
    HF page before their pool."""

    def __init__(self, *, base_url: str = "https://huggingface.co") -> None:
        self.base_url = base_url.rstrip("/")
        self.http = HttpClient(base_url=self.base_url)

    def close(self) -> None:
        self.http.close()

    def fetch(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        data = self.http.get_json("/api/trending")
        if not isinstance(data, dict):
            return output
        for model in (data.get("recentlyTrending") or [])[:10]:
            model_id = str(model.get("repoData") and model.get("repoData").get("id") or "")
            if not model_id:
                model_id = str(model.get("modelId") or "")
            if not model_id:
                continue
            output.append(
                {
                    "title": model_id,
                    "text": f"{model_id} {model.get('summary') or ''}".strip(),
                    "url": f"https://huggingface.co/{model_id}",
                    # H14: the trending payload carries repoData.lastModified
                    # (verified against the live API); fall back to None (the
                    # engine then stamps crawl time) only when absent.
                    "published": _iso((model.get("repoData") or {}).get("lastModified")),
                    "author": str((model.get("repoData") or {}).get("author") or ""),
                    "source_domain": "huggingface.co",
                    "metrics": {"downloads": model.get("downloads", 0)},
                }
            )
        return output


def normalize_telegram_message(message: Any, *, channel_handle: str) -> dict[str, Any] | None:
    """Turn a Telethon Message into a narrative mention payload (pure, testable)."""
    text = str(getattr(message, "message", "") or "").strip()
    if not text:
        return None
    message_id = getattr(message, "id", None)
    date = _dt(getattr(message, "date", None))
    handle = channel_handle.lstrip("@")
    url = (
        f"https://t.me/{handle}/{message_id}"
        if message_id is not None
        else f"https://t.me/{handle}"
    )
    return {
        "title": text[:256],
        "text": text,
        "url": url,
        "published": date,
        "author": str(getattr(message, "sender_id", "") or ""),
        "source_domain": "t.me",
        "metrics": {
            "channel": channel_handle,
            "views": getattr(message, "views", None),
            "forwards": getattr(message, "forwards", None),
        },
    }


class TelegramCrawler:
    """ToS-safe public-channel crawler via Telethon (MTProto, free).

    Only public broadcast channels are read: ``entity.username`` must be set and
    ``entity.broadcast`` must be true, so private chats and groups are never
    touched. Rate limiting is a pause between channels plus Telethon's built-in
    flood-sleep handling. Requires a one-time interactive login to create the
    session file (see ``scripts/telegram_auth.py``).
    """

    def __init__(
        self,
        channel_handles: list[str],
        *,
        api_id: int,
        api_hash: str,
        session_file: str,
        message_limit: int = 30,
        pause_seconds: float = 2.0,
    ) -> None:
        self.channel_handles = channel_handles
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_file = session_file
        self.message_limit = message_limit
        self.pause_seconds = pause_seconds

    def close(self) -> None:
        return None

    def fetch(self) -> list[dict[str, Any]]:
        if not _TELEGRAM_AVAILABLE or not self.channel_handles:
            return []
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[dict[str, Any]]:
        client = _TelegramClient(
            self.session_file,
            self.api_id,
            self.api_hash,
            flood_sleep_threshold=60,
        )
        output: list[dict[str, Any]] = []
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError(
                    "Telegram session is not authorized; run scripts/telegram_auth.py once "
                    "to complete the interactive login."
                )
            for handle in self.channel_handles:
                try:
                    entity = await client.get_entity(handle)
                    if not self._is_public_channel(entity):
                        log.info("telegram_channel_skipped", channel=handle, reason="not_public")
                        continue
                    messages = await client.get_messages(entity, limit=self.message_limit)
                    for message in messages:
                        normalized = normalize_telegram_message(message, channel_handle=handle)
                        if normalized:
                            output.append(normalized)
                except Exception as exc:  # noqa: BLE001 - preserve per-channel failure.
                    log.warning("telegram_channel_failed", channel=handle, error=str(exc))
                finally:
                    await asyncio.sleep(max(0.0, self.pause_seconds))
        finally:
            await client.disconnect()
        return output

    @staticmethod
    def _is_public_channel(entity: Any) -> bool:
        username = getattr(entity, "username", None)
        broadcast = getattr(entity, "broadcast", False)
        return bool(username) and bool(broadcast)


class RSSNewsCrawler:
    """Public RSS/news feeds for catalyst and narrative signal."""

    def __init__(self, feed_urls: list[str]) -> None:
        self.feed_urls = feed_urls
        self.http = HttpClient()

    def close(self) -> None:
        self.http.close()

    def fetch(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for url in self.feed_urls:
            response = self.http._client.get(url)
            response.raise_for_status()
            feed = feedparser.parse(response.text)
            for entry in feed.entries:
                title = str(getattr(entry, "title", "") or "").strip()
                if not title:
                    continue
                link = str(getattr(entry, "link", "") or "")
                output.append(
                    {
                        "title": title,
                        "text": title,
                        "url": link,
                        "published": _rss_published(entry),
                        "author": "",
                        "source_domain": url.split("/")[2] if "//" in url else url,
                        "metrics": {"feed": url},
                    }
                )
        return output
