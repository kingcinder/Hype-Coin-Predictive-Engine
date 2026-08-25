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
