"""Single-writer serialization queue for the engine database.

SQLite allows exactly one writer at a time. The engine runs the ingestion
worker loop, the REST API, and background probes in different threads; two
threads committing overlapping write transactions contend for SQLite's single
write lock and wedge the loop on ``"database is locked"`` — the wedge the
watchdog used to paper over with process restarts.

This module serializes write transactions through one dedicated writer thread:

- WAL mode (``PRAGMA journal_mode=WAL``, set per-connection in
  :mod:`storage.database`) lets readers use separate connections concurrently
  with the writer, so reads never block on writes.
- Callers submit a callable ``fn(session) -> T`` via :meth:`WriteQueue.submit`
  / :meth:`WriteQueue.submit_sync`; the writer thread executes it with its own
  dedicated session, commits, and hands the result back through a
  :class:`~concurrent.futures.Future`.
- ``fn`` owns the writes but not the transaction boundary: it may
  ``session.flush()``, but must not ``commit()``/``rollback()``/``close()``
  the session — the queue commits on success and rolls back on error.
- Returned values must be plain data (ids, dicts, primitives) — never live
  ORM instances, which stay bound to the writer thread's session.
- Wedge observability: queue depth, write counts, error counts, and write
  latency (p50/p99 over a sliding window, split into queue-wait vs execution)
  are tracked in-process and periodically persisted to ``SystemHealth``
  (``component="write_queue"``) by the writer thread itself.
- :meth:`WriteQueue.stop` drains the queue: every submitted task completes
  before the thread joins, so shutdown loses no point-in-time evidence.

Readers are unchanged: they keep opening their own sessions
(:func:`storage.database.session_scope` / ``SessionLocal``) on any thread.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.orm import Session

from common.logging import get_logger
from common.time import utc_now

log = get_logger(__name__)

T = TypeVar("T")

#: SystemHealth component name used for the queue's own telemetry rows.
COMPONENT = "write_queue"

_LATENCY_WINDOW = 2048
_HEARTBEAT_POLL_SEC = 1.0

# SystemHealth state thresholds for the heartbeat row.
_P99_YELLOW_SEC = 0.5
_QUEUE_DEPTH_YELLOW = 1000

_SENTINEL = object()


class WriteQueueNotRunning(RuntimeError):
    """Raised when a write is submitted but no write queue is running."""


@dataclass
class _WriteTask:
    fn: Callable[[Session], Any]
    submitted_at: float
    future: Future[Any]


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, int(pct * len(ordered)))
    return ordered[idx]


class WriteQueue:
    """Serializes DB write transactions through one dedicated writer thread."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        *,
        heartbeat_interval_sec: float = 60.0,
        heartbeat_every_n_writes: int = 1000,
    ) -> None:
        # ``session_factory`` resolves lazily at start() so this module has no
        # import-time dependency on storage.database (avoids import cycles via
        # storage/__init__.py).
        self._session_factory = session_factory
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._heartbeat_every_n_writes = heartbeat_every_n_writes
        self._queue: queue.Queue[_WriteTask | object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._writer_session: Session | None = None
        self._running = False
        self._stopping = False
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._writes_total = 0
        self._writes_failed = 0
        self._exec_latencies: deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._wait_latencies: deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._last_heartbeat_at = 0.0
        self._writes_since_heartbeat = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> WriteQueue:
        """Start the writer thread. Idempotent."""
        with self._lock:
            if self._running:
                return self
            if self._session_factory is None:
                from storage.database import SessionLocal

                self._session_factory = SessionLocal
            factory = self._session_factory
            self._started_at = time.monotonic()
            self._last_heartbeat_at = self._started_at
            self._stopping = False
            self._running = True
        self._verify_wal_mode(factory)
        self._thread = threading.Thread(target=self._writer_loop, name="write-queue", daemon=True)
        self._thread.start()
        log.info("write_queue_started")
        return self

    def stop(self, timeout: float | None = None) -> bool:
        """Drain the queue and stop the writer thread.

        The sentinel is queued FIFO behind every submitted task, so every
        accepted write commits before the thread joins — shutdown loses no
        evidence. Returns True when the thread stopped cleanly.
        """
        with self._lock:
            if not self._running:
                return True
            self._stopping = True
        self._queue.put(_SENTINEL)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            alive = thread.is_alive()
        else:
            alive = False
        with self._lock:
            self._running = False
            self._thread = None
        if alive:
            log.error("write_queue_stop_timeout", timeout=timeout)
            return False
        log.info(
            "write_queue_stopped",
            writes_total=self._writes_total,
            writes_failed=self._writes_failed,
        )
        return True

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def is_writer_thread(self) -> bool:
        """True when called on the queue's own writer thread."""
        return threading.current_thread() is self._thread

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------
    def submit(self, fn: Callable[[Session], T]) -> Future[T]:
        """Queue ``fn(session)`` for execution on the writer thread.

        Returns a Future for the result. Re-entrant: calling from the writer
        thread itself executes inline against the writer session instead of
        deadlocking on the queue.
        """
        future: Future[T] = Future()
        if self.is_writer_thread():
            # Already serialized — run inline on the writer's own session.
            try:
                assert self._writer_session is not None
                future.set_result(fn(self._writer_session))
            except Exception as exc:  # noqa: BLE001
                future.set_exception(exc)
            return future
        with self._lock:
            running = self._running and not self._stopping
            thread_alive = self._thread is not None and self._thread.is_alive()
        if not running or not thread_alive:
            raise WriteQueueNotRunning(
                "write queue is not running; start it before submitting writes"
            )
        self._queue.put(_WriteTask(fn=fn, submitted_at=time.monotonic(), future=future))
        return future

    def submit_sync(self, fn: Callable[[Session], T], timeout: float | None = None) -> T:
        """Submit ``fn`` and block until it completes (or ``timeout``)."""
        return self.submit(fn).result(timeout=timeout)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """Point-in-time queue stats for logging / health checks."""
        with self._lock:
            exec_lat = list(self._exec_latencies)
            wait_lat = list(self._wait_latencies)
            return {
                "queue_depth": self._queue.qsize(),
                "writes_total": self._writes_total,
                "writes_failed": self._writes_failed,
                "exec_latency_p50_sec": _percentile(exec_lat, 0.50),
                "exec_latency_p99_sec": _percentile(exec_lat, 0.99),
                "queue_wait_p50_sec": _percentile(wait_lat, 0.50),
                "queue_wait_p99_sec": _percentile(wait_lat, 0.99),
                "uptime_sec": time.monotonic() - self._started_at if self._started_at else 0.0,
                "running": self._running,
                "writer_alive": self._thread is not None and self._thread.is_alive(),
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _verify_wal_mode(self, factory: Callable[[], Session]) -> None:
        """Log the effective journal mode; WAL is what de-wedges readers."""
        try:
            with factory() as session:
                mode = session.execute(text("PRAGMA journal_mode")).scalar()
            log.info("write_queue_journal_mode", journal_mode=mode)
            if isinstance(mode, str) and mode.lower() != "wal":
                log.warning(
                    "write_queue_not_wal",
                    journal_mode=mode,
                    detail="storage.database sets PRAGMA journal_mode=WAL per "
                    "connection; a non-WAL mode here means that pragma did not "
                    "apply (e.g. a non-SQLite backend or an exotic URL).",
                )
        except Exception as exc:  # noqa: BLE001 - never block boot on the check.
            log.warning("write_queue_journal_mode_check_failed", error=str(exc))

    def _writer_loop(self) -> None:
        factory = self._session_factory
        assert factory is not None
        session = factory()
        self._writer_session = session
        try:
            while True:
                try:
                    item = self._queue.get(timeout=_HEARTBEAT_POLL_SEC)
                except queue.Empty:
                    self._maybe_heartbeat(session, force=False)
                    continue
                if item is _SENTINEL:
                    self._queue.task_done()
                    break
                assert isinstance(item, _WriteTask)
                self._execute(item, session)
                self._queue.task_done()
                self._maybe_heartbeat(session, force=False)
            # Final heartbeat so the drained state is visible in SystemHealth.
            self._maybe_heartbeat(session, force=True)
        except Exception as exc:  # noqa: BLE001 - thread death must be loud.
            log.exception("write_queue_thread_died", error=str(exc))
        finally:
            self._writer_session = None
            try:
                session.close()
            except Exception:  # noqa: BLE001, S110 - best effort on the way out.
                pass

    def _execute(self, task: _WriteTask, session: Session) -> None:
        wait_sec = time.monotonic() - task.submitted_at
        start = time.monotonic()
        try:
            result = task.fn(session)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - rollback is best effort.
                pass
            with self._lock:
                self._writes_failed += 1
            if not task.future.done():
                task.future.set_exception(exc)
            log.warning("write_queue_task_failed", error=str(exc))
            return
        exec_sec = time.monotonic() - start
        with self._lock:
            self._writes_total += 1
            self._writes_since_heartbeat += 1
            self._exec_latencies.append(exec_sec)
            self._wait_latencies.append(wait_sec)
        if not task.future.done():
            task.future.set_result(result)

    def _maybe_heartbeat(self, session: Session, *, force: bool) -> None:
        now = time.monotonic()
        due_time = (now - self._last_heartbeat_at) >= self._heartbeat_interval_sec
        due_count = self._writes_since_heartbeat >= self._heartbeat_every_n_writes
        if not (force or due_time or due_count):
            return
        self._last_heartbeat_at = now
        self._writes_since_heartbeat = 0
        try:
            from storage.repository import (
                record_health,  # noqa: PLC0415 - lazy, avoids import cycle.
            )

            snapshot = self.stats()
            p99 = snapshot["exec_latency_p99_sec"]
            degraded = p99 > _P99_YELLOW_SEC or snapshot["queue_depth"] > _QUEUE_DEPTH_YELLOW
            state = "yellow" if degraded else "ok"
            record_health(
                session,
                component=COMPONENT,
                state=state,
                message=(
                    f"writes={snapshot['writes_total']} "
                    f"failed={snapshot['writes_failed']} "
                    f"depth={snapshot['queue_depth']} "
                    f"exec_p50={snapshot['exec_latency_p50_sec'] * 1000:.1f}ms "
                    f"exec_p99={p99 * 1000:.1f}ms "
                    f"wait_p99={snapshot['queue_wait_p99_sec'] * 1000:.1f}ms "
                    f"uptime={snapshot['uptime_sec']:.0f}s"
                ),
                lag_sec=p99,
                error_count=snapshot["writes_failed"],
                ts=utc_now(),
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - telemetry must never kill the writer.
            log.warning("write_queue_heartbeat_failed", error=str(exc))
            try:
                session.rollback()
            except Exception:  # noqa: BLE001, S110 - best effort.
                pass


# ----------------------------------------------------------------------
# Module-level singleton: the engine owns exactly one write queue.
# ----------------------------------------------------------------------
_default_queue: WriteQueue | None = None
_default_lock = threading.Lock()


def start_write_queue(
    session_factory: Callable[[], Session] | None = None,
    **kwargs: Any,
) -> WriteQueue:
    """Start (or return) the process-wide write queue. Idempotent."""
    global _default_queue
    with _default_lock:
        if _default_queue is not None and _default_queue.is_running():
            return _default_queue
        _default_queue = WriteQueue(session_factory=session_factory, **kwargs)
    _default_queue.start()
    return _default_queue


def get_write_queue() -> WriteQueue | None:
    """The process-wide write queue, or None when it was never started."""
    with _default_lock:
        q = _default_queue
    return q if q is not None and q.is_running() else None


def require_write_queue() -> WriteQueue:
    """The process-wide write queue; raises when it is not running."""
    q = get_write_queue()
    if q is None:
        raise WriteQueueNotRunning(
            "no write queue is running; call start_write_queue() first "
            "(engine/run.py starts it with the engine lifecycle)"
        )
    return q


def stop_write_queue(timeout: float | None = None) -> bool:
    """Drain and stop the process-wide write queue. Idempotent."""
    global _default_queue
    with _default_lock:
        q = _default_queue
        _default_queue = None
    if q is None:
        return True
    return q.stop(timeout=timeout)


__all__ = [
    "COMPONENT",
    "WriteQueue",
    "WriteQueueNotRunning",
    "get_write_queue",
    "require_write_queue",
    "start_write_queue",
    "stop_write_queue",
]
