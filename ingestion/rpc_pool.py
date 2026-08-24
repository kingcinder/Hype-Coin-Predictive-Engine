from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from sqlalchemy.orm import Session

from common.config import get_settings
from common.time import utc_now
from storage import models
from storage.repository import record_health

HEALTH_START = 1.0
HEALTH_FAILURE_DECAY = 0.25
HEALTH_SUCCESS_BOOST = 0.05
HEALTH_DOWN = 0.0
PROBE_EVERY_PICKS = 20


@dataclass(frozen=True)
class ProbeRecord:
    ts: datetime
    ok: bool


@dataclass(frozen=True)
class EndpointState:
    url: str
    health: float
    consecutive_failures: int
    down: bool
    last_probe_at: datetime | None = None
    last_probe_ok: bool | None = None
    probe_count: int = 0
    probe_successes: int = 0
    probe_failures: int = 0
    probe_history: tuple[ProbeRecord, ...] = ()


@dataclass(frozen=True)
class RpcPoolAlert:
    """A deduplicated operational notification emitted by one chain pool."""

    chain_slug: str
    kind: str
    url: str | None
    healthy_endpoints: int
    total_endpoints: int
    down_for_seconds: float | None = None


class RpcEndpointPool:
    """Curated free RPC endpoints with health scores and automatic failover.

    Blueprint §2: "maintain a curated endpoint pool with health scores and
    automatic failover." Every request marks the active endpoint a success or a
    failure; a failure decays its health (so a healthier endpoint wins the next
    pick), and ``failure_threshold`` consecutive failures take it down —
    excluded from ``pick``. A downed endpoint is re-probed every
    ``PROBE_EVERY_PICKS`` picks (the probe is a real request); when it succeeds
    it jump-starts to ``recovery_health`` and rejoins the pool. ``pick`` prefers
    the healthiest endpoint with a least-recently-used tiebreak so traffic
    spreads across equally healthy endpoints. Thread-safe.
    """

    def __init__(
        self,
        endpoints: list[str],
        *,
        failure_threshold: int = 2,
        recovery_health: float = 0.55,
        chain_slug: str = "unknown",
    ) -> None:
        deduped: list[str] = []
        for url in endpoints:
            if url not in deduped:
                deduped.append(url)
        self.endpoints: tuple[str, ...] = tuple(deduped)
        self.chain_slug = chain_slug
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_health = min(1.0, max(0.0, recovery_health))
        self._health: dict[str, float] = {url: HEALTH_START for url in self.endpoints}
        self._consecutive_failures: dict[str, int] = {url: 0 for url in self.endpoints}
        self._last_used: dict[str, float] = {url: 0.0 for url in self.endpoints}
        self._tick = 0.0
        self._lock = threading.Lock()
        self._background_thread: threading.Thread | None = None
        self._background_stop = threading.Event()
        self._background_active = False
        self._alert_callback: Callable[[RpcPoolAlert], bool] | None = None
        self._down_since: dict[str, datetime] = {}
        self._down_alerted: set[str] = set()
        self._alert_inflight: set[str] = set()
        self._zero_healthy_alerted = False
        self._probe_history: dict[str, deque[ProbeRecord]] = {
            url: deque(maxlen=20) for url in self.endpoints
        }
        self._probe_counts: dict[str, int] = {url: 0 for url in self.endpoints}
        self._probe_successes: dict[str, int] = {url: 0 for url in self.endpoints}

    @property
    def enabled(self) -> bool:
        return len(self.endpoints) > 0

    def pick(self) -> str:
        """Best endpoint: highest health, down endpoints excluded, LRU tiebreak.

        While the background probe thread is not running, every
        ``PROBE_EVERY_PICKS`` picks forces a downed endpoint through so it can
        recover. If everything is down, the least-recently-used endpoint is
        chosen so the pool keeps trying rather than failing hard.
        """
        if not self.endpoints:
            raise RuntimeError("RPC pool has no endpoints")
        with self._lock:
            self._tick += 1.0
            down = [
                url
                for url in self.endpoints
                if self._is_down(url)
            ]
            available = [url for url in self.endpoints if url not in down]
            if down and not self._background_active and self._tick % PROBE_EVERY_PICKS == 0:
                # Pick-probe slot: force one downed endpoint through so it can
                # recover instead of being blacklisted forever (only used when
                # the background probe thread is not running).
                chosen = min(down, key=lambda url: self._last_used[url])
            elif available:
                chosen = min(
                    available,
                    key=lambda url: (
                        -self._health[url],
                        self._last_used[url],
                    ),
                )
            else:
                chosen = min(self.endpoints, key=lambda url: self._last_used[url])
            self._last_used[chosen] = self._tick
            return chosen

    def mark_success(self, url: str) -> None:
        with self._lock:
            if url not in self._health:
                return
            if self._is_down(url):
                # Recovered from the down state: jump-start above the recovery
                # threshold so it can win picks again instead of trickling back.
                self._health[url] = max(
                    self.recovery_health,
                    min(1.0, self._health[url] + HEALTH_SUCCESS_BOOST),
                )
            else:
                self._health[url] = min(1.0, self._health[url] + HEALTH_SUCCESS_BOOST)
            self._consecutive_failures[url] = 0

    def mark_failure(self, url: str) -> None:
        with self._lock:
            if url not in self._health:
                return
            self._health[url] = max(HEALTH_DOWN, self._health[url] - HEALTH_FAILURE_DECAY)
            self._consecutive_failures[url] += 1
            if self._is_down(url):
                self._down_since.setdefault(url, utc_now())

    def health(self, url: str) -> float:
        with self._lock:
            return self._health.get(url, HEALTH_DOWN)

    def snapshot(self) -> list[EndpointState]:
        with self._lock:
            return [
                EndpointState(
                    url=url,
                    health=round(self._health[url], 4),
                    consecutive_failures=self._consecutive_failures[url],
                    down=self._is_down(url),
                    last_probe_at=(
                        self._probe_history[url][-1].ts if self._probe_history[url] else None
                    ),
                    last_probe_ok=(
                        self._probe_history[url][-1].ok if self._probe_history[url] else None
                    ),
                    probe_count=self._probe_counts[url],
                    probe_successes=self._probe_successes[url],
                    probe_failures=self._probe_counts[url] - self._probe_successes[url],
                    probe_history=tuple(self._probe_history[url]),
                )
                for url in self.endpoints
            ]

    def _is_down(self, url: str) -> bool:
        return self._consecutive_failures[url] >= self.failure_threshold

    def _record_probe(self, url: str, ok: bool) -> None:
        with self._lock:
            record = ProbeRecord(ts=utc_now(), ok=ok)
            self._probe_history[url].append(record)
            self._probe_counts[url] += 1
            if ok:
                self._probe_successes[url] += 1

    def _sync_alert_state_locked(self, now: datetime) -> None:
        for url in self.endpoints:
            if self._is_down(url):
                self._down_since.setdefault(url, now)
            else:
                self._down_since.pop(url, None)
                self._down_alerted.discard(url)
                self._alert_inflight.discard(f"endpoint:{url}")
        if any(not self._is_down(url) for url in self.endpoints):
            self._zero_healthy_alerted = False
            self._alert_inflight.discard("zero_healthy")

    def _notification_events_locked(
        self,
        *,
        now: datetime,
        cooldown_seconds: float,
    ) -> list[tuple[str, RpcPoolAlert]]:
        self._sync_alert_state_locked(now)
        healthy = sum(not self._is_down(url) for url in self.endpoints)
        events: list[tuple[str, RpcPoolAlert]] = []
        if self.endpoints and healthy == 0 and not self._zero_healthy_alerted:
            key = "zero_healthy"
            if key not in self._alert_inflight:
                self._alert_inflight.add(key)
                events.append(
                    (
                        key,
                        RpcPoolAlert(
                            chain_slug=self.chain_slug,
                            kind="zero_healthy_endpoints",
                            url=None,
                            healthy_endpoints=healthy,
                            total_endpoints=len(self.endpoints),
                        ),
                    )
                )
        for url, down_since in self._down_since.items():
            down_for = max(0.0, (now - down_since).total_seconds())
            key = f"endpoint:{url}"
            if (
                down_for >= max(0.0, cooldown_seconds)
                and url not in self._down_alerted
                and key not in self._alert_inflight
            ):
                self._alert_inflight.add(key)
                events.append(
                    (
                        key,
                        RpcPoolAlert(
                            chain_slug=self.chain_slug,
                            kind="endpoint_down_cooldown",
                            url=url,
                            healthy_endpoints=healthy,
                            total_endpoints=len(self.endpoints),
                            down_for_seconds=down_for,
                        ),
                    )
                )
        return events

    def dispatch_alerts(
        self,
        callback: Callable[[RpcPoolAlert], bool],
        *,
        cooldown_seconds: float,
        now: datetime | None = None,
    ) -> int:
        """Deliver newly eligible pool alerts, retaining failed deliveries.

        The callback returns True only after ntfy accepted the message. Failed
        delivery leaves the event eligible for the next scan/probe pass.
        """
        current = now or utc_now()
        with self._lock:
            events = self._notification_events_locked(
                now=current, cooldown_seconds=cooldown_seconds
            )
        sent = 0
        for key, event in events:
            delivered = False
            try:
                delivered = bool(callback(event))
            except Exception:  # noqa: BLE001 - alert delivery must not break RPC work.
                delivered = False
            with self._lock:
                self._alert_inflight.discard(key)
                if delivered:
                    if key == "zero_healthy":
                        self._zero_healthy_alerted = True
                    elif event.url is not None:
                        self._down_alerted.add(event.url)
                    sent += 1
        return sent

    # ------------------------------------------------------- background probing

    def start_background_probe(
        self,
        probe: Callable[[str], bool],
        *,
        interval_seconds: float,
        alert_callback: Callable[[RpcPoolAlert], bool] | None = None,
    ) -> bool:
        """Start a daemon thread that probes downed endpoints on a timer.

        Idempotent: a second call while the thread is alive is a no-op. The
        thread wakes every ``interval_seconds`` and probes every currently-down
        endpoint, marking success/failure so recovered endpoints rejoin the
        pool without waiting for traffic. Returns True if the thread is (or was
        already) running.
        """
        with self._lock:
            if self._background_thread is not None and self._background_thread.is_alive():
                return True
            self._alert_callback = alert_callback
            self._background_active = True
            self._background_stop.clear()
            thread = threading.Thread(
                target=self._probe_loop,
                args=(probe, max(0.05, interval_seconds)),
                name="rpc-pool-probe",
                daemon=True,
            )
            self._background_thread = thread
        thread.start()
        return True

    def stop_background_probe(self) -> None:
        """Stop the probe thread (if running) and wait for it to exit."""
        with self._lock:
            self._background_active = False
            self._background_stop.set()
            thread = self._background_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            if self._background_thread is thread and (
                thread is None or not thread.is_alive()
            ):
                self._background_thread = None

    def _probe_loop(self, probe: Callable[[str], bool], interval_seconds: float) -> None:
        try:
            while not self._background_stop.wait(interval_seconds):
                self.probe_down_endpoints(probe)
                callback = self._alert_callback
                if callback is not None:
                    self.dispatch_alerts(
                        callback,
                        cooldown_seconds=get_settings().rpc_pool_alert_cooldown_seconds,
                    )
        finally:
            # Keep lifecycle state accurate if the daemon exits while the
            # owning process is shutting down, not only when stop() joins it.
            with self._lock:
                if self._background_thread is threading.current_thread():
                    self._background_thread = None
                    self._background_active = False

    def _probe_urls(self, urls: list[str], probe: Callable[[str], bool]) -> int:
        for url in urls:
            ok = False
            try:
                ok = bool(probe(url))
            except Exception:  # noqa: BLE001 - a probe must never raise.
                ok = False
            if ok:
                self.mark_success(url)
            else:
                self.mark_failure(url)
            self._record_probe(url, ok)
        return len(urls)

    def probe_endpoints(self, probe: Callable[[str], bool]) -> int:
        """Probe every endpoint, used before a source crawl batch."""
        return self._probe_urls(list(self.endpoints), probe)

    def probe_down_endpoints(self, probe: Callable[[str], bool]) -> int:
        """Synchronously probe every currently-down endpoint.

        Successful probes mark the endpoint recovered (jump-start to
        ``recovery_health``); failed probes decay its health further. Returns
        the number of endpoints probed. Safe to call from any thread.
        """
        down = [state.url for state in self.snapshot() if state.down]
        return self._probe_urls(down, probe)


POOL_CHAINS = ("solana", "base", "ethereum")


@lru_cache(maxsize=16)
def get_rpc_pool(chain_slug: str = "solana") -> RpcEndpointPool:
    settings = get_settings()
    return RpcEndpointPool(
        settings.rpc_pool_endpoints(chain_slug),
        failure_threshold=settings.rpc_pool_failure_threshold,
        recovery_health=settings.rpc_pool_recovery_health,
        chain_slug=chain_slug,
    )



def _pool_health_row(session: Session, chain_slug: str) -> None:
    pool = get_rpc_pool(chain_slug)
    states = pool.snapshot()
    if not states:
        return
    down = [state.url for state in states if state.down]
    degraded = [
        state.url for state in states if state.health < HEALTH_START and not state.down
    ]
    state = "red" if down else ("yellow" if degraded else "ok")
    message = (
        f"{chain_slug} rpc pool: {len(states)} endpoints, {len(down)} down, "
        f"{len(degraded)} degraded"
    )
    if down:
        message = f"{message}; down={down[0]}"
    elif degraded:
        message = f"{message}; degraded={degraded[0]}"
    record_health(
        session,
        component=f"rpc_pool:{chain_slug}",
        state=state,
        message=message,
        error_count=len(down),
    )


def persist_pool_snapshots(session: Session, *, ts: datetime | None = None) -> int:
    """Persist one endpoint snapshot per chain for cross-process API reads."""
    settings = get_settings()
    if not settings.rpc_pool_enabled:
        return 0
    snapshot_ts = ts or utc_now()
    count = 0
    for chain_slug in POOL_CHAINS:
        for state in get_rpc_pool(chain_slug).snapshot():
            session.add(
                models.RpcPoolSnapshot(
                    chain_slug=chain_slug,
                    url=state.url,
                    ts=snapshot_ts,
                    health=state.health,
                    consecutive_failures=state.consecutive_failures,
                    down=state.down,
                    last_probe_at=state.last_probe_at,
                    last_probe_ok=state.last_probe_ok,
                    probe_count=state.probe_count,
                    probe_successes=state.probe_successes,
                    probe_failures=state.probe_failures,
                    probe_history=[
                        {"ts": probe.ts.isoformat(), "ok": probe.ok}
                        for probe in state.probe_history
                    ],
                )
            )
            count += 1
    session.flush()
    return count


def record_pool_health(session: Session) -> None:
    """Persist one `component:rpc_pool:{chain}` health row per configured chain."""
    settings = get_settings()
    if not settings.rpc_pool_enabled:
        return
    for chain_slug in POOL_CHAINS:
        _pool_health_row(session, chain_slug)
