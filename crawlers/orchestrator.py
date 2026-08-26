"""Night Crawler Orchestrator — manages all crawlers, scheduling, health, adaptive frequency.

Coordinates the entire army of crawlers: manages scheduling based on adaptive
frequencies, tracks health across all sources, records signal quality metrics,
and feeds results into the data lake pipeline.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from common.config import get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from crawlers.base import BaseCrawler
from crawlers.heuristics import HeuristicsEngine
from crawlers.sources.cmc import CMCCrawler
from crawlers.sources.coingecko import CoinGeckoCrawler
from crawlers.sources.coinpaprika import CoinPaprikaCrawler
from crawlers.sources.defillama import DeFiLlamaCrawler
from crawlers.sources.dexscreener_trends import DexScreenerTrendsCrawler
from crawlers.sources.explorer import ExplorerCrawler
from crawlers.sources.farcaster import FarcasterCrawler
from crawlers.sources.gas_tracker import GasTrackerCrawler
from crawlers.sources.github_trending import GitHubTrendingCrawler
from crawlers.sources.google_trends import GoogleTrendsCrawler
from crawlers.sources.nitter import NitterCrawler
from crawlers.sources.presale import PresaleCrawler
from crawlers.sources.pump_fun import PumpFunCrawler
from crawlers.sources.pump_portal import PumpPortalCrawler
from crawlers.sources.whale_tracker import WhaleTrackerCrawler
from crawlers.sources.x_trends import XTrendsCrawler
from engine.activity_stream import broadcast_activity, compute_activity_signal_score
from storage.repository import get_or_create_source, record_health, store_raw_evidence

log = get_logger(__name__)


class NightCrawlerOrchestrator:
    """Manages the army of Night Crawlers.

    - Discovers and registers all crawlers
    - Runs them with adaptive frequency based on heuristics
    - Tracks health and signal quality
    - Feeds results into the data lake pipeline
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.heuristics = HeuristicsEngine()
        self._crawlers: dict[str, BaseCrawler] = {}
        self._last_run: dict[str, datetime] = {}
        self._lock = threading.Lock()
        self._init_crawlers()

    def _init_crawlers(self) -> None:
        """Initialize all available crawlers."""
        self._crawlers["coingecko"] = CoinGeckoCrawler()
        if self.settings.nightcrawler_cmc_enabled:
            self._crawlers["coinmarketcap"] = CMCCrawler()
        self._crawlers["pump_fun"] = PumpFunCrawler()
        self._crawlers["defillama"] = DeFiLlamaCrawler()
        self._crawlers["whale_tracker"] = WhaleTrackerCrawler()
        self._crawlers["explorer"] = ExplorerCrawler(
            etherscan_api_key=self.settings.etherscan_api_key
        )
        self._crawlers["nitter"] = NitterCrawler()
        self._crawlers["presale"] = PresaleCrawler()
        if self.settings.nightcrawler_gas_tracker_enabled:
            self._crawlers["gas_tracker"] = GasTrackerCrawler(
                etherscan_api_key=self.settings.etherscan_api_key,
                include_solana=self.settings.gas_tracker_solana_enabled,
            )
        if self.settings.nightcrawler_coinpaprika_enabled:
            self._crawlers["coinpaprika"] = CoinPaprikaCrawler()
        if self.settings.nightcrawler_github_trending_enabled:
            self._crawlers["github_trending"] = GitHubTrendingCrawler(
                search_query=self.settings.github_trending_search_query,
                token=self.settings.github_token,
            )
        if self.settings.nightcrawler_x_trends_enabled:
            self._crawlers["x_trends"] = XTrendsCrawler(
                keywords=self.settings.x_trends_crypto_keywords_csv.split(",")
            )
        if self.settings.nightcrawler_pump_portal_enabled:
            self._crawlers["pump_portal"] = PumpPortalCrawler()
        if self.settings.nightcrawler_dexscreener_trends_enabled:
            self._crawlers["dexscreener_trends"] = DexScreenerTrendsCrawler()
        if self.settings.nightcrawler_google_trends_enabled:
            self._crawlers["google_trends"] = GoogleTrendsCrawler(
                geo=self.settings.google_trends_geo
            )
        if self.settings.nightcrawler_farcaster_enabled:
            farcaster_queries = self.settings.farcaster_search_queries_csv.split(",")
            self._crawlers["farcaster"] = FarcasterCrawler(
                search_queries=[q.strip() for q in farcaster_queries if q.strip()],
                api_key=self.settings.farcaster_api_key,
                tracked_fids=self.settings.farcaster_tracked_fids,
                tracked_channels=self.settings.farcaster_tracked_channels,
            )

    def run_all(
        self,
        session: Session,
        *,
        decision_ts: datetime | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run all crawlers that are due, with adaptive frequency.

        Each crawler runs independently — a crash in one does not abort the rest.
        The lock is held only for scheduling decisions and _last_run writes;
        HTTP I/O (crawler.fetch) happens inside a per-crawler try/except so a
        single failure cannot block the entire fleet.
        """
        decision_ts = ensure_utc(decision_ts or utc_now())
        started = time.monotonic()
        results: dict[str, Any] = {}

        # Snapshot the set of crawlers and which are due — release lock before I/O
        with self._lock:
            due: list[tuple[str, BaseCrawler]] = []
            for name, crawler in self._crawlers.items():
                if self._should_run(name, force):
                    due.append((name, crawler))
                else:
                    results[name] = {"status": "skipped", "reason": "not_due"}

        for name, crawler in due:
            try:
                log.info("nightcrawler_start", name=name)
                items = crawler.fetch()

                with self._lock:
                    self._last_run[name] = decision_ts

                # Store raw evidence
                source = get_or_create_source(
                    session,
                    name=f"nightcrawler:{name}",
                    source_type="nightcrawler",
                    tier="enriched",
                    base_url=None,
                )
                if items:
                    store_raw_evidence(
                        session,
                        source=source,
                        payload={"items": items, "count": len(items)},
                        observed_at=decision_ts,
                        raw_path=f"nightcrawler:{name}:{decision_ts.isoformat()}",
                    )
                    # Link items to known assets so cross-source fusion sees
                    # this crawler as a corroborating source when scoring.
                    try:
                        from crawlers.signal_links import link_crawler_items

                        link_crawler_items(
                            session,
                            source=source,
                            items=items,
                            observed_at=decision_ts,
                        )
                    except Exception:  # noqa: BLE001
                        pass  # linking is additive, never breaks the crawler
                    # Broadcast to WebSocket clients for live feed updates
                    try:
                        sig_score, sig_engagement, sig_mentions = compute_activity_signal_score(
                            items
                        )
                        broadcast_activity(
                            source=name,
                            items=items,
                            item_count=len(items),
                            signal_score=sig_score,
                            token_mentions=sig_mentions[:5],
                            engagement=sig_engagement,
                            observed_at=str(decision_ts)[:19],
                        )
                    except Exception:  # noqa: BLE001
                        pass  # never let broadcast failures block the crawler

                results[name] = {
                    "status": "ok",
                    "items": len(items),
                    "health": {
                        "reliability": round(crawler.health.reliability_score, 3),
                        "total_runs": crawler.health.total_runs,
                        "error_rate": round(crawler.health.error_rate, 3),
                    },
                }

                # Feed signal quality into heuristics
                self.heuristics.analyze_source_reliability(session, name)

                # Record health
                record_health(
                    session,
                    component=f"nightcrawler:{name}",
                    state="ok" if items else "yellow",
                    message=f"{len(items)} items collected",
                )
            except Exception as exc:  # noqa: BLE001 — one crash must not abort the fleet
                log.exception("nightcrawler_crawler_failed", crawler=name, error=str(exc))
                results[name] = {
                    "status": "error",
                    "error": str(exc),
                    "items": 0,
                }
                record_health(
                    session,
                    component=f"nightcrawler:{name}",
                    state="red",
                    message=str(exc),
                    error_count=1,
                )

        # Prune stale heuristics to prevent unbounded memory growth
        try:
            prune_result = self.heuristics.prune(max_age_days=30)
            results["_prune"] = prune_result
        except Exception as exc:  # noqa: BLE001 — pruning failure must not abort the fleet.
            log.debug("heuristics_prune_failed", error=str(exc))

        duration = time.monotonic() - started
        total_items = sum(r.get("items", 0) for r in results.values() if isinstance(r, dict))

        record_health(
            session,
            component="nightcrawler_orchestrator",
            state="ok",
            message=(f"{len(results)} crawlers, {total_items} total items, {duration:.1f}s"),
        )

        return {
            "crawlers_run": len(
                [r for r in results.values() if isinstance(r, dict) and r.get("status") == "ok"]
            ),
            "total_items": total_items,
            "duration_sec": round(duration, 2),
            "details": results,
            "heuristics": self.heuristics.summarize(),
        }

    def _should_run(self, name: str, force: bool) -> bool:
        """Determine if a crawler should run based on adaptive frequency."""
        if force:
            return True

        with self._lock:
            last = self._last_run.get(name)
        if last is None:
            return True

        # Base interval from config
        base_interval = timedelta(minutes=self.settings.nightcrawler_interval_minutes)

        # Adaptive multiplier from heuristics
        multiplier = self.heuristics.get_crawl_frequency_multiplier(name)

        interval = base_interval / max(0.25, multiplier)
        return (utc_now() - ensure_utc(last)) >= interval

    def close(self) -> None:
        """Close all crawler HTTP clients (thread-safe)."""
        with self._lock:
            crawlers_snapshot = list(self._crawlers.values())
        for crawler in crawlers_snapshot:
            crawler.close()

    def get_status(self) -> dict[str, Any]:
        """Get status of all crawlers."""
        with self._lock:
            last_run_snapshot = dict(self._last_run)
        return {
            name: {
                "enabled": crawler.health.enabled,
                "reliability": round(crawler.health.reliability_score, 3),
                "total_runs": crawler.health.total_runs,
                "total_items": crawler.health.total_items,
                "error_rate": round(crawler.health.error_rate, 3),
                "last_run": str(last_run_snapshot.get(name, "never"))[:19],
                "frequency_multiplier": round(
                    self.heuristics.get_crawl_frequency_multiplier(name), 2
                ),
            }
            for name, crawler in self._crawlers.items()
        }


# Module-level singleton with lock
_nightcrawler_orchestrator: NightCrawlerOrchestrator | None = None
_orchestrator_lock = threading.Lock()


def get_nightcrawler_orchestrator() -> NightCrawlerOrchestrator:
    global _nightcrawler_orchestrator
    if _nightcrawler_orchestrator is None:
        with _orchestrator_lock:
            if _nightcrawler_orchestrator is None:
                _nightcrawler_orchestrator = NightCrawlerOrchestrator()
    return _nightcrawler_orchestrator


def close_nightcrawler_orchestrator() -> None:
    """Close and reset the singleton orchestrator. Called on engine shutdown."""
    global _nightcrawler_orchestrator
    with _orchestrator_lock:
        if _nightcrawler_orchestrator is not None:
            _nightcrawler_orchestrator.close()
            _nightcrawler_orchestrator = None


def run_nightcrawlers(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Entry point: run all Night Crawlers."""
    orchestrator = get_nightcrawler_orchestrator()
    try:
        return orchestrator.run_all(session, decision_ts=decision_ts, force=force)
    except Exception as exc:
        log.exception("nightcrawler_orchestrator_failed", error=str(exc))
        record_health(
            session,
            component="nightcrawler_orchestrator",
            state="red",
            message=str(exc),
            error_count=1,
        )
        return {"error": str(exc)}
