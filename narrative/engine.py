from __future__ import annotations

import hashlib
from datetime import datetime
from functools import lru_cache, partial
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from ingestion.rpc_pool import RpcEndpointPool
from narrative.cluster import cluster_mentions
from narrative.crawlers import (
    GitHubCrawler,
    HuggingFaceCrawler,
    RedditCrawler,
    RSSNewsCrawler,
    TelegramCrawler,
    YouTubeCrawler,
)
from narrative.embed import MinhashEmbedder
from storage import models
from storage.repository import (
    get_or_create_source,
    record_health,
    store_raw_evidence,
    upsert_news_item,
    upsert_social_mention,
)

log = get_logger(__name__)

_NARRATIVE_PROBE_PATHS = {
    "github": "/rate_limit",
    "huggingface": "/api/trending",
}


@lru_cache(maxsize=16)
def get_narrative_endpoint_pool(
    source_name: str,
    endpoints: tuple[str, ...],
    failure_threshold: int,
) -> RpcEndpointPool:
    return RpcEndpointPool(
        list(endpoints),
        failure_threshold=failure_threshold,
        chain_slug=f"narrative:{source_name}",
    )


def probe_narrative_endpoint(source_name: str, url: str, path: str) -> bool:
    """Fast-fail health check for a narrative source endpoint."""
    try:
        params = {"limit": 1} if source_name == "reddit" else None
        with httpx.Client(timeout=5.0) as client:
            response = client.get(
                f"{url.rstrip('/')}{path}",
                params=params,
                headers={"User-Agent": "serpent-hype-coin-engine/0.1"},
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        return isinstance(payload, (dict, list))
    except Exception:  # noqa: BLE001 - a dead source must be skipped, not fatal.
        return False


_SOCIAL_CRAWLERS = (
    ("reddit", "reddit_public", "social", "https://www.reddit.com"),
    ("youtube", "youtube_rss", "social", None),
    ("github", "github_public", "public_metadata", "https://api.github.com"),
    ("huggingface", "huggingface", "public_metadata", "https://huggingface.co"),
    ("telegram", "telegram", "social", None),
)


class NarrativeEngine:
    def __init__(self) -> None:
        self.settings = get_settings()

    def crawl(self, session: Session, *, decision_ts: datetime | None = None) -> dict[str, int]:
        decision_ts = ensure_utc(decision_ts or utc_now())
        counts: dict[str, int] = {}
        for name, source_name, source_type, base_url in _SOCIAL_CRAWLERS:
            crawler_settings = self._crawler_settings(name)
            if crawler_settings is None:
                continue
            endpoint_pool, endpoint_url = self._prepare_endpoint_pool(
                name, crawler_settings.get("probe_path")
            )
            if endpoint_pool is not None and endpoint_url is None:
                source = get_or_create_source(
                    session,
                    name=source_name,
                    source_type=source_type,
                    tier="social",
                    base_url=base_url,
                )
                record_health(
                    session,
                    component=f"source:{source.name}",
                    state="yellow",
                    message="all configured endpoints are down; crawl batch skipped",
                    error_count=1,
                )
                counts[name] = 0
                continue
            crawler = self._social_crawler(name, endpoint_url=endpoint_url)
            if crawler is None:
                continue
            try:
                counts[name] = self._crawl_social(
                    session,
                    crawler=crawler,
                    source_name=source_name,
                    source_type=source_type,
                    base_url=base_url,
                    endpoint_pool=endpoint_pool,
                    endpoint_url=endpoint_url,
                    decision_ts=decision_ts,
                )
            finally:
                crawler.close()
        counts["rss"] = self._crawl_rss(session, decision_ts=decision_ts)
        return counts

    def _crawler_settings(self, name: str) -> dict[str, str] | None:
        if name == "reddit":
            if not self.settings.reddit_subreddits:
                return None
            subreddit = self.settings.reddit_subreddits[0]
            return {"probe_path": f"/r/{subreddit}/new.json"}
        if name == "youtube":
            return {"probe_path": ""} if self.settings.youtube_channels else None
        if name == "github":
            return (
                {"probe_path": _NARRATIVE_PROBE_PATHS["github"]}
                if self.settings.github_search_queries
                else None
            )
        if name == "huggingface":
            return (
                {"probe_path": _NARRATIVE_PROBE_PATHS["huggingface"]}
                if self.settings.hf_trending_enabled
                else None
            )
        if name == "telegram":
            return {"probe_path": ""} if self._telegram_crawler() is not None else None
        return None

    def _prepare_endpoint_pool(
        self, source_name: str, probe_path: str | None
    ) -> tuple[RpcEndpointPool | None, str | None]:
        endpoints = tuple(self.settings.narrative_endpoint_pools.get(source_name, []))
        if not self.settings.narrative_endpoint_pool_enabled or not endpoints:
            return None, None
        pool = get_narrative_endpoint_pool(
            source_name,
            endpoints,
            max(1, self.settings.narrative_endpoint_failure_threshold),
        )
        if probe_path:
            probe = partial(probe_narrative_endpoint, source_name, path=probe_path)
            pool.probe_endpoints(probe)
            if self.settings.narrative_background_probe_enabled:
                pool.start_background_probe(
                    probe,
                    interval_seconds=self.settings.narrative_probe_interval_seconds,
                )
        healthy = [state.url for state in pool.snapshot() if not state.down]
        return pool, pool.pick() if healthy else None

    def _social_crawler(self, name: str, *, endpoint_url: str | None = None):
        if name == "reddit":
            subreddits = self.settings.reddit_subreddits
            return (
                RedditCrawler(subreddits, base_url=endpoint_url or "https://www.reddit.com")
                if subreddits
                else None
            )
        if name == "youtube":
            channels = self.settings.youtube_channels
            return YouTubeCrawler(channels) if channels else None
        if name == "github":
            queries = self.settings.github_search_queries
            return (
                GitHubCrawler(queries, base_url=endpoint_url or "https://api.github.com")
                if queries
                else None
            )
        if name == "huggingface":
            return (
                HuggingFaceCrawler(base_url=endpoint_url or "https://huggingface.co")
                if self.settings.hf_trending_enabled
                else None
            )
        if name == "telegram":
            return self._telegram_crawler()
        return None

    def _telegram_crawler(self) -> TelegramCrawler | None:
        if not self.settings.telegram_enabled:
            return None
        if not self.settings.telegram_api_id or not self.settings.telegram_api_hash:
            log.info(
                "telegram_disabled",
                reason="TELEGRAM_API_ID or TELEGRAM_API_HASH not configured",
            )
            return None
        handles = self.settings.telegram_channel_handles
        if not handles:
            return None
        return TelegramCrawler(
            handles,
            api_id=self.settings.telegram_api_id,
            api_hash=self.settings.telegram_api_hash,
            session_file=self.settings.telegram_session_file,
            message_limit=self.settings.telegram_message_limit,
            pause_seconds=self.settings.telegram_rate_limit_pause_seconds,
        )

    def _crawl_social(
        self,
        session: Session,
        *,
        crawler,
        source_name: str,
        source_type: str,
        base_url: str | None,
        endpoint_pool: RpcEndpointPool | None,
        endpoint_url: str | None,
        decision_ts: datetime,
    ) -> int:
        source = get_or_create_source(
            session, name=source_name, source_type=source_type, tier="social", base_url=base_url
        )
        try:
            try:
                items = crawler.fetch()
            except Exception:
                if endpoint_pool is not None and endpoint_url is not None:
                    endpoint_pool.mark_failure(endpoint_url)
                raise
            if endpoint_pool is not None and endpoint_url is not None:
                endpoint_pool.mark_success(endpoint_url)
            store_raw_evidence(
                session,
                source=source,
                payload={"items": items},
                observed_at=decision_ts,
                raw_path=f"narrative:{source_name}:{decision_ts.isoformat()}",
            )
            stored = 0
            for item in items:
                asset = self._resolve_asset(session, item.get("title") or "")
                ts = item.get("published") or decision_ts
                upsert_social_mention(
                    session,
                    asset_id=asset.id if asset else None,
                    topic=(item.get("title") or "")[:256],
                    source_id=source.id,
                    ts=ts,
                    observed_at=decision_ts,
                    author_hash=(
                        hashlib.sha256(str(item.get("author") or "").encode()).hexdigest()
                        if item.get("author")
                        else None
                    ),
                    metrics_json=item.get("metrics") or {},
                    raw_ref=item.get("url") or f"{source_name}:{ts.isoformat()}",
                )
                stored += 1
            record_health(
                session,
                component=f"source:{source_name}",
                state="ok",
                message=f"{stored} mentions",
            )
            return stored
        except Exception as exc:  # noqa: BLE001 - preserve per-source failure.
            log.warning("narrative_crawl_failed", source=source_name, error=str(exc))
            record_health(
                session,
                component=f"source:{source_name}",
                state="red",
                message=str(exc),
                error_count=1,
            )
            return 0

    def _crawl_rss(self, session: Session, *, decision_ts: datetime) -> int:
        feed_urls = self.settings.rss_feed_urls
        if not feed_urls:
            return 0
        source = get_or_create_source(
            session,
            name="rss_news",
            source_type="news",
            tier="public_metadata",
            base_url=None,
        )
        try:
            crawler = RSSNewsCrawler(feed_urls)
            try:
                items = crawler.fetch()
            finally:
                crawler.close()
            store_raw_evidence(
                session,
                source=source,
                payload={"items": items},
                observed_at=decision_ts,
                raw_path=f"narrative:rss:{decision_ts.isoformat()}",
            )
            stored = 0
            for item in items:
                title = item.get("title") or ""
                asset = self._resolve_asset(session, title)
                if not asset:
                    continue
                upsert_news_item(
                    session,
                    source_id=source.id,
                    published_at=item.get("published"),
                    observed_at=decision_ts,
                    source_domain=str(item.get("source_domain") or "unknown"),
                    title=title,
                    url=item.get("url") or f"rss:{decision_ts.isoformat()}",
                )
                stored += 1
            record_health(
                session,
                component="source:rss_news",
                state="ok",
                message=f"{stored} news items",
            )
            return stored
        except Exception as exc:  # noqa: BLE001
            log.warning("rss_crawl_failed", error=str(exc))
            record_health(
                session,
                component="source:rss_news",
                state="red",
                message=str(exc),
                error_count=1,
            )
            return 0

    def cluster(self, session: Session, *, decision_ts: datetime | None = None) -> int:
        decision_ts = ensure_utc(decision_ts or utc_now())
        try:
            embedder = MinhashEmbedder(self.settings.narrative_cluster_sig_size)
            clustered = cluster_mentions(
                session,
                decision_ts=decision_ts,
                embedder=embedder,
                threshold=self.settings.narrative_cluster_threshold,
            )
            record_health(
                session,
                component="narrative_clusters",
                state="ok",
                message=f"{clustered} mentions clustered",
            )
            return clustered
        except Exception as exc:  # noqa: BLE001
            log.exception("narrative_cluster_failed", error=str(exc))
            record_health(
                session,
                component="narrative_clusters",
                state="red",
                message=str(exc),
                error_count=1,
            )
            return 0

    def _resolve_asset(self, session: Session, text: str) -> models.Asset | None:
        lowered = text.lower()
        assets = session.scalars(select(models.Asset)).all()
        for asset in assets:
            symbol = asset.symbol.lower()
            if symbol and len(symbol) >= 2 and symbol in lowered:
                return asset
        return None


def run_narrative(session: Session, *, decision_ts: datetime | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.narrative_crawl_enabled:
        return {"skipped": True}
    decision_ts = ensure_utc(decision_ts or utc_now())
    engine = NarrativeEngine()
    counts = engine.crawl(session, decision_ts=decision_ts)
    counts["clustered"] = engine.cluster(session, decision_ts=decision_ts)
    return counts
