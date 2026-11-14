"""Shared pacing and circuit breaking for rate-limited ingestion providers."""

from __future__ import annotations

import threading
import time

RATE_LIMIT_STATUSES = frozenset({403, 429})


class CircuitOpen(RuntimeError):
    """Raised when a provider is in its cooldown window."""


class ProviderGate:
    """Serializes calls to a provider and opens a circuit on rate limits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._blocked_until = 0.0
        self._min_interval = 0.0
        self._cooldown = 60.0

    def configure(self, *, min_interval: float, cooldown: float) -> None:
        with self._lock:
            self._min_interval = max(0.0, min_interval)
            self._cooldown = max(0.0, cooldown)

    def before(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._blocked_until:
                raise CircuitOpen(f"provider cooling down for {self._blocked_until - now:.1f}s")
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._min_interval
        if wait > 0:
            time.sleep(wait)

    def record_failure(self, exc: BaseException) -> None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status not in RATE_LIMIT_STATUSES:
            return
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + self._cooldown)

    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._blocked_until

    def reset(self) -> None:
        with self._lock:
            self._next_at = 0.0
            self._blocked_until = 0.0
