"""Base crawler class — shared patterns for all Night Crawlers.

Provides retry logic, rate limiting, health tracking, deduplication,
and a standard interface for the orchestrator to manage crawlers uniformly.
"""

from __future__ import annotations

import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from common.http import build_httpx_client
from common.logging import get_logger
from common.time import utc_now

log = get_logger(__name__)


@dataclass
class CrawlerHealth:
    """Health state for a single crawler."""

    name: str
    enabled: bool = True
    total_runs: int = 0
    total_items: int = 0
    total_errors: int = 0
    consecutive_errors: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None
    last_duration_ms: float = 0.0
    avg_duration_ms: float = 0.0
    error_rate: float = 0.0
    reliability_score: float = 1.0  # 0.0 = unreliable, 1.0 = perfect
    priority_score: float = 1.0  # higher = crawl more often
    consecutive_empty_runs: int = 0  # runs that succeeded but returned 0 items

    def record_success(self, item_count: int, duration_ms: float) -> None:
        self.total_runs += 1
        self.total_items += item_count
        self.consecutive_errors = 0
        self.consecutive_empty_runs = 0
        self.last_run_at = utc_now()
        self.last_duration_ms = duration_ms
        self.last_error = None
        # Exponential moving average of duration
        alpha = 0.3
        self.avg_duration_ms = alpha * duration_ms + (1 - alpha) * self.avg_duration_ms
        # Update error rate and reliability
        self.error_rate = self.total_errors / max(1, self.total_runs)
        self.reliability_score = max(0.1, 1.0 - self.error_rate)

    def record_empty_run(self, duration_ms: float) -> None:
        """Record a run that completed without transport errors but produced
        zero items (H26).

        Repeated empty runs degrade the reliability score — a fleet-wide
        transport failure used to look like perfect health because per-source
        ``except Exception -> return []`` paths were recorded as successes.
        Empty runs are *not* hard errors: they never trip the ``is_healthy``
        skip gate, so a legitimately quiet source is deprioritized, not
        silenced. Any non-empty run resets the streak via ``record_success``.
        """
        self.total_runs += 1
        self.consecutive_empty_runs += 1
        self.last_run_at = utc_now()
        self.last_duration_ms = duration_ms
        alpha = 0.3
        self.avg_duration_ms = alpha * duration_ms + (1 - alpha) * self.avg_duration_ms
        self.reliability_score = max(0.1, self.reliability_score - 0.15)
        self.error_rate = self.total_errors / max(1, self.total_runs)

    def record_error(self, error: str) -> None:
        self.total_runs += 1
        self.total_errors += 1
        self.consecutive_errors += 1
        self.last_run_at = utc_now()
        self.last_error = error
        self.error_rate = self.total_errors / max(1, self.total_runs)
        self.reliability_score = max(0.1, 1.0 - self.error_rate)

    @property
    def is_healthy(self) -> bool:
        return self.enabled and self.consecutive_errors < 5

    @property
    def effective_priority(self) -> float:
        """Priority adjusted by reliability — unreliable crawlers get deprioritized."""
        return self.priority_score * self.reliability_score


class BaseCrawler(ABC):
    """Abstract base class for all Night Crawlers.

    Subclasses implement ``fetch_items()`` which returns a list of raw items.
    The base class handles retry, rate limiting, deduplication, and health tracking.
    """

    def __init__(
        self,
        name: str,
        *,
        max_retries: int = 2,
        retry_delay_seconds: float = 2.0,
        rate_limit_pause: float = 0.5,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.name = name
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.rate_limit_pause = rate_limit_pause
        self.timeout_seconds = timeout_seconds
        self.health = CrawlerHealth(name=name)
        self._seen_hashes: set[str] = set()
        self._client: httpx.Client | None = None

    def _create_client_headers(self) -> dict[str, str]:
        """Return HTTP headers for the client. Subclasses can override to add custom headers."""
        return {"User-Agent": "serpent-nightcrawler/1.0"}

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            # Centralized construction with an explicit, sanitized proxy policy
            # (H26): a hostile proxy environment must never crash this, and any
            # residual construction failure raises here so fetch() records it
            # as an error instead of a silent empty success.
            self._client = build_httpx_client(
                timeout=self.timeout_seconds,
                headers=self._create_client_headers(),
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client and not self._client.is_closed:
            self._client.close()

    @abstractmethod
    def fetch_items(self) -> list[dict[str, Any]]:
        """Fetch raw items from the source. Subclasses implement this."""
        ...

    def fetch(self) -> list[dict[str, Any]]:
        """Main entry point: fetch with retry, dedup, and health tracking."""
        if not self.health.is_healthy:
            log.info("crawler_skipped", name=self.name, reason="unhealthy")
            return []

        start = time.monotonic()
        last_error: str | None = None

        for attempt in range(self.max_retries + 1):
            try:
                # Fail fast on transport-layer construction (H26): per-source
                # fetch_items() implementations swallow exceptions into []/None,
                # which used to be recorded as success-with-zero-items while the
                # whole HTTP layer was down. Touching the client here keeps any
                # construction failure attributable, so it flows into the retry
                # loop and record_error() below instead of a bogus success.
                self.client  # noqa: B018 - intentional fail-fast touch
                items = self.fetch_items()
                # Deduplicate
                unique_items = self._deduplicate(items)
                duration_ms = (time.monotonic() - start) * 1000
                if unique_items:
                    self.health.record_success(len(unique_items), duration_ms)
                else:
                    # Success-with-zero-items degrades reliability (H26): N
                    # consecutive empty runs push reliability toward 0.1 via
                    # record_empty_run, but never trip the is_healthy skip gate.
                    self.health.record_empty_run(duration_ms)
                    log.info(
                        "crawler_empty",
                        name=self.name,
                        raw=len(items),
                        consecutive_empty=self.health.consecutive_empty_runs,
                        reliability=round(self.health.reliability_score, 3),
                    )
                log.info(
                    "crawler_success",
                    name=self.name,
                    items=len(unique_items),
                    raw=len(items),
                    duration_ms=round(duration_ms, 1),
                    attempt=attempt + 1,
                )
                time.sleep(self.rate_limit_pause)
                return unique_items
            except Exception as exc:
                last_error = str(exc)
                log.warning(
                    "crawler_error",
                    name=self.name,
                    attempt=attempt + 1,
                    error=last_error,
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay_seconds * (attempt + 1))

        duration_ms = (time.monotonic() - start) * 1000
        self.health.record_error(last_error or "unknown error")
        self.health.last_duration_ms = duration_ms
        return []

    def _deduplicate(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate items based on content hash."""
        unique: list[dict[str, Any]] = []
        for item in items:
            h = self._item_hash(item)
            if h not in self._seen_hashes:
                self._seen_hashes.add(h)
                unique.append(item)
        # Prevent unbounded memory growth
        if len(self._seen_hashes) > 50_000:
            self._seen_hashes = set(list(self._seen_hashes)[-25_000:])
        return unique

    def _item_hash(self, item: dict[str, Any]) -> str:
        """Generate a content hash for deduplication."""
        key = f"{item.get('title', '')}{item.get('url', '')}{item.get('text', '')[:200]}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]
