"""Live crawler activity streaming via WebSocket — bridges sync worker → async WS clients.

The night crawler orchestrator writes activity events via ``broadcast_activity()``;
the FastAPI WebSocket handler drains its queue into the WS frame stream.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from common.logging import get_logger

log = get_logger(__name__)


class _ActivityStreamBroker:
    """Thread-safe broadcaster that bridges sync crawler → async WebSocket endpoints."""

    # M8: bound per-subscriber queues (slow clients drop events instead of
    # growing memory forever) and the subscriber count itself.
    _MAX_QUEUE_SIZE = 1000
    _MAX_SUBSCRIBERS = 256

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []
        # Event loop each queue is drained on (captured in connect(), which
        # runs on the loop thread). Broadcasts from foreign threads schedule
        # the put via loop.call_soon_threadsafe (M4) instead of calling
        # put_nowait() directly, which is unsafe when a getter is pending.
        self._loops: dict[int, asyncio.AbstractEventLoop] = {}
        self._lock = threading.Lock()

    def connect(self) -> asyncio.Queue[dict[str, Any]]:
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # connected outside a running loop (tests)
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._MAX_QUEUE_SIZE)
        with self._lock:
            if len(self._queues) >= self._MAX_SUBSCRIBERS:
                oldest = self._queues.pop(0)
                self._loops.pop(id(oldest), None)
            self._queues.append(q)
            if loop is not None:
                self._loops[id(q)] = loop
        return q

    def disconnect(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._queues = [x for x in self._queues if x is not q]
            self._loops.pop(id(q), None)

    @staticmethod
    def _enqueue(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # slow client — drop the event

    def broadcast(self, event: dict[str, Any]) -> None:
        """Called from any thread; puts the event into every connected queue."""
        with self._lock:
            targets = [(q, self._loops.get(id(q))) for q in self._queues]
        dead: list[int] = []
        for q, loop in targets:
            if loop is not None:
                try:
                    # Thread-safe: runs put_nowait on the loop's own thread.
                    loop.call_soon_threadsafe(self._enqueue, q, event)
                    continue
                except RuntimeError:
                    # The subscriber's loop closed mid-broadcast: the handler
                    # is gone, so drop the event and untrack the queue rather
                    # than risking a foreign-thread put on a dead loop.
                    dead.append(id(q))
                    continue
            self._enqueue(q, event)
        if dead:
            dead_set = set(dead)
            with self._lock:
                self._queues = [x for x in self._queues if id(x) not in dead_set]
                for qid in dead_set:
                    self._loops.pop(qid, None)

    @property
    def connected_count(self) -> int:
        with self._lock:
            return len(self._queues)


# Module-level singleton
activity_stream_broker = _ActivityStreamBroker()


def compute_activity_signal_score(items: list[dict[str, Any]]) -> tuple[float, int, list[str]]:
    """Shared signal-score computation used by the orchestrator, activity API,
    and leaderboard API.  Returns (signal_score, total_engagement, token_mentions).
    """
    total_engagement = 0
    seen_tokens: set[str] = set()
    token_mentions: list[str] = []
    for item in items[:20]:
        metrics = item.get("metrics", {})
        total_engagement += metrics.get("engagement_score", 0)
        total_engagement += metrics.get("likes", 0)
        total_engagement += metrics.get("recasts", 0) * 2
        total_engagement += metrics.get("replies", 0)
        for mention in metrics.get("token_mentions", []):
            if mention not in seen_tokens:
                seen_tokens.add(mention)
                token_mentions.append(mention)
    signal_score = min(100, total_engagement * 2 + len(items) * 2)
    return signal_score, total_engagement, token_mentions


def broadcast_activity(
    source: str,
    items: list[dict[str, Any]],
    *,
    item_count: int = 0,
    signal_score: float = 0.0,
    token_mentions: list[str] | None = None,
    engagement: int = 0,
    platform: str = "",
    observed_at: str = "",
) -> None:
    """Broadcast a crawler activity event to all connected WebSocket clients.

    Called from the orchestrator after storing raw evidence.
    Thread-safe; never blocks the crawler worker.
    """
    try:
        activity_stream_broker.broadcast(
            {
                "type": "activity",
                "source": source,
                "item_count": item_count or len(items),
                "signal_score": signal_score,
                "token_mentions": token_mentions or [],
                "total_engagement": engagement,
                "platform": platform,
                "observed_at": observed_at,
                "items": items[:5],  # send at most 5 items to keep payloads small
            }
        )
    except Exception:  # noqa: BLE001
        pass  # never let WS failures block the crawler
