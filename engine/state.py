"""Shared in-memory engine runtime state.

The worker loop (main thread) writes to this; the FastAPI process (daemon
thread) reads from it to power the ``/engine/status`` and ``/engine/progress``
endpoints.  Because the engine runs in a single process the module-level
singleton is sufficient — no IPC is needed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from common.logging import get_logger

log = get_logger(__name__)


# ── SSE event broadcaster ────────────────────────────────────────────────────
# Each connected SSE client gets an asyncio.Queue.  The worker thread (which
# calls the mark_* mutators) enqueues a snapshot via ``broadcast_event()``;
# the FastAPI SSE handler drains its queue into the HTTP response stream.


class _SSEBroker:
    """Thread-safe broadcaster that bridges sync worker → async SSE endpoints."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = threading.Lock()

    def connect(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._lock:
            self._queues.append(q)
        return q

    def disconnect(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._queues = [x for x in self._queues if x is not q]

    def broadcast(self, event: dict[str, Any]) -> None:
        """Called from any thread; puts the event into every connected queue."""
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client — drop the event


sse_broker = _SSEBroker()


class ScanPhase(StrEnum):
    IDLE = "idle"
    BOOTSTRAPPING = "bootstrapping"
    SCANNING = "scanning"
    FORECASTING = "forecasting"
    RETENTION = "retention"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class ScanProgress:
    """Per-scan progress snapshot."""

    phase: ScanPhase = ScanPhase.IDLE
    phase_message: str = ""
    started_at: float | None = None
    completed_at: float | None = None
    iteration: int = 0
    total_pairs: int = 0
    total_scores: int = 0
    total_forecasts: int = 0
    total_lifecycle: int = 0
    total_narrative: int = 0
    total_catalysts: int = 0
    total_ignition_events: int = 0
    total_fingerprints: int = 0
    total_archive: int = 0
    total_ntfy: int = 0
    total_rpc_snapshots: int = 0
    error_message: str | None = None


@dataclass
class EngineState:
    """Global engine runtime state singleton."""

    # Lifecycle
    started_at: float | None = None
    scan_interval_seconds: int = 300
    total_iterations: int = 0

    # Current scan
    scan: ScanProgress = field(default_factory=ScanProgress)

    # Lock for thread-safe updates
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- Mutators (called from the worker thread) ---

    def mark_bootstrapping(self) -> None:
        with self._lock:
            self.scan = ScanProgress(
                phase=ScanPhase.BOOTSTRAPPING,
                phase_message="Bootstrapping database and reference data",
                started_at=time.monotonic(),
            )
        self._notify_sse("bootstrapping")

    def mark_scanning(
        self, iteration: int | None = None, message: str = "Running ingestion scan"
    ) -> None:
        with self._lock:
            if iteration is not None:
                self.scan.iteration = iteration
            self.scan.phase = ScanPhase.SCANNING
            self.scan.phase_message = message
            self.scan.started_at = time.monotonic()
            self.scan.completed_at = None
        self._notify_sse("scanning")

    def mark_forecasting(self) -> None:
        with self._lock:
            self.scan.phase = ScanPhase.FORECASTING
            self.scan.phase_message = "Training forecast model"
        self._notify_sse("forecasting")

    def mark_retention(self) -> None:
        with self._lock:
            self.scan.phase = ScanPhase.RETENTION
            self.scan.phase_message = "Running retention autopilot"
        self._notify_sse("retention")

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        """Safely convert a value to int, handling dicts and nested structures.

        Duplicate of ingestion.service._int_from_result kept here to avoid
        circular imports. Both handle the same dict shapes:
        - lifecycle / lp_removals: {events: N}
        - narrative: {reddit: 0, github: 30, ...}
        - mempool: {solana: {watched: 5}, evm: {pairs: 0}}
        - archive: {compacted: 0, partitions: 0, ...}
        """
        if isinstance(value, dict):
            if not value:
                return default
            if "events" in value:
                return int(value["events"])
            # mempool / nested dicts — sum watched across chains
            if any(isinstance(v, dict) for v in value.values()):
                total = 0
                for v in value.values():
                    if isinstance(v, dict):
                        total += int(v.get("watched", 0))
                if total > 0:
                    return total
            # archive / other — try partitions or count
            for key in ("partitions", "count", "compacted"):
                if key in value:
                    return int(value[key])
            # narrative — sum all numeric values
            numeric_sum = sum(v for v in value.values() if isinstance(v, (int, float)))
            if numeric_sum > 0:
                return int(numeric_sum)
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def mark_scan_result(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.scan.total_pairs = self._safe_int(result.get("pairs"))
            self.scan.total_scores = self._safe_int(result.get("scores"))
            self.scan.total_forecasts = self._safe_int(result.get("forecasts"))
            self.scan.total_lifecycle = self._safe_int(result.get("lifecycle"))
            self.scan.total_narrative = self._safe_int(result.get("narrative"))
            self.scan.total_catalysts = self._safe_int(result.get("catalysts"))
            self.scan.total_ignition_events = self._safe_int(result.get("ignition_events"))
            self.scan.total_fingerprints = self._safe_int(result.get("fingerprints"))
            self.scan.total_archive = self._safe_int(result.get("archive"))
            ntfy = result.get("ntfy")
            self.scan.total_ntfy = (
                self._safe_int(ntfy.get("sent", 0)) if isinstance(ntfy, dict) else 0
            )
            self.scan.total_rpc_snapshots = self._safe_int(result.get("rpc_pool_snapshots"))
        self._notify_sse("scan_progress")

    def mark_completed(self) -> None:
        with self._lock:
            self.scan.phase = ScanPhase.COMPLETED
            self.scan.phase_message = "Scan complete"
            self.scan.completed_at = time.monotonic()
            self.total_iterations += 1
        self._notify_sse("completed")

    def mark_error(self, error: str) -> None:
        with self._lock:
            self.scan.phase = ScanPhase.ERROR
            self.scan.phase_message = f"Error: {error}"
            self.scan.error_message = error
            self.scan.completed_at = time.monotonic()
        self._notify_sse("error")

    # --- SSE notification (called from worker thread after lock release) ---

    def _notify_sse(self, event_type: str) -> None:
        """Broadcast a state-change event to all connected SSE clients."""
        try:
            sse_broker.broadcast({"type": event_type, **self.snapshot()})
        except Exception:  # noqa: BLE001
            pass  # never let SSE failures block the worker

    # --- Accessors (called from API thread) ---

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the current state."""
        with self._lock:
            uptime_sec = (time.monotonic() - self.started_at) if self.started_at else None
            scan_duration = (
                (self.scan.completed_at - self.scan.started_at)
                if self.scan.started_at and self.scan.completed_at
                else ((time.monotonic() - self.scan.started_at) if self.scan.started_at else None)
            )
            return {
                "status": "running" if self.started_at else "stopped",
                "uptime_sec": round(uptime_sec, 1) if uptime_sec is not None else None,
                "total_iterations": self.total_iterations,
                "scan_interval_seconds": self.scan_interval_seconds,
                "scan": {
                    "phase": self.scan.phase.value,
                    "phase_message": self.scan.phase_message,
                    "started_at": self.scan.started_at,
                    "completed_at": self.scan.completed_at,
                    "duration_sec": round(scan_duration, 1) if scan_duration is not None else None,
                    "iteration": self.scan.iteration,
                    "pairs": self.scan.total_pairs,
                    "scores": self.scan.total_scores,
                    "forecasts": self.scan.total_forecasts,
                    "lifecycle": self.scan.total_lifecycle,
                    "narrative": self.scan.total_narrative,
                    "catalysts": self.scan.total_catalysts,
                    "ignition_events": self.scan.total_ignition_events,
                    "fingerprints": self.scan.total_fingerprints,
                    "archive": self.scan.total_archive,
                    "ntfy_sent": self.scan.total_ntfy,
                    "rpc_pool_snapshots": self.scan.total_rpc_snapshots,
                    "error_message": self.scan.error_message,
                },
            }


# Module-level singleton — shared between worker and API threads.
engine_state = EngineState()
