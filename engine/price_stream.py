"""Live price streaming via WebSocket — bridges sync worker → async WS clients.

The ingestion worker writes price snapshots via ``broadcast_price()``; the
FastAPI WebSocket handler drains its queue into the WS frame stream.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

from common.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class PriceUpdate:
    """A single price update for a token pair."""

    asset_id: int
    symbol: str
    chain: str
    address: str
    price_usd: float | None
    volume_usd: float | None
    liquidity_usd: float | None
    timestamp: str  # ISO format

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "chain": self.chain,
            "address": self.address,
            "price_usd": self.price_usd,
            "volume_usd": self.volume_usd,
            "liquidity_usd": self.liquidity_usd,
            "timestamp": self.timestamp,
        }


class _PriceStreamBroker:
    """Thread-safe broadcaster that bridges sync worker → async WebSocket endpoints."""

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
price_stream_broker = _PriceStreamBroker()


def broadcast_price(update: PriceUpdate) -> None:
    """Broadcast a price update to all connected WebSocket clients.

    Called from the ingestion worker after collecting market snapshots.
    Thread-safe; never blocks the worker.
    """
    try:
        price_stream_broker.broadcast(
            {
                "type": "price_update",
                **update.to_dict(),
            }
        )
    except Exception:  # noqa: BLE001
        pass  # never let WS failures block the worker
