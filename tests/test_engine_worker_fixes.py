"""Regression tests for the engine/worker fixes.

M4: brokers bridge sync threads -> async queues via loop.call_soon_threadsafe.
M5: ingestion worker --loop isolates per-stage exceptions.
M6: engine watchdog phases abandon promptly on shutdown (stop-aware join).
M8: per-subscriber queues and subscriber counts are bounded.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from engine.activity_stream import _ActivityStreamBroker
from engine.price_stream import _PriceStreamBroker
from engine.state import _SSEBroker

_BROKERS = [_SSEBroker, _ActivityStreamBroker, _PriceStreamBroker]


# ── M4: thread-safe cross-thread broadcast ────────────────────────────────


@pytest.mark.parametrize("broker_cls", _BROKERS)
def test_broker_broadcast_from_foreign_thread(broker_cls) -> None:
    """A queue connected on a loop thread receives broadcasts from another thread."""
    broker = broker_cls()
    box: dict = {}
    ready = threading.Event()

    def loop_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        ready.set()
        loop.run_forever()

    async def _connect() -> asyncio.Queue:
        return broker.connect()

    thread = threading.Thread(target=loop_thread, daemon=True)
    thread.start()
    assert ready.wait(timeout=10)
    loop: asyncio.AbstractEventLoop = box["loop"]
    # connect() must execute ON the running loop, as the FastAPI handlers do.
    queue: asyncio.Queue = asyncio.run_coroutine_threadsafe(_connect(), loop).result(
        timeout=10
    )
    try:
        # The loop recorded at connect() time must be the draining loop.
        assert broker._loops[id(queue)] is loop  # noqa: SLF001 - white-box check

        getter = asyncio.run_coroutine_threadsafe(queue.get(), loop)
        broker.broadcast({"type": "ping"})  # from this (foreign) thread
        assert getter.result(timeout=10) == {"type": "ping"}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)


@pytest.mark.parametrize("broker_cls", _BROKERS)
def test_broker_broadcast_without_running_loop_falls_back(broker_cls) -> None:
    """connect() outside a running loop still yields a working queue."""
    broker = broker_cls()
    queue = broker.connect()
    broker.broadcast({"type": "x"})
    assert queue.get_nowait() == {"type": "x"}


@pytest.mark.parametrize("broker_cls", _BROKERS)
def test_broker_broadcast_drops_queue_whose_loop_closed(broker_cls) -> None:
    """A subscriber whose loop closed mid-broadcast is untracked and its event
    dropped — never a foreign-thread put onto a dead loop's queue."""
    broker = broker_cls()
    box: dict = {}
    ready = threading.Event()

    def loop_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        ready.set()
        loop.run_forever()

    async def _connect() -> asyncio.Queue:
        return broker.connect()

    thread = threading.Thread(target=loop_thread, daemon=True)
    thread.start()
    assert ready.wait(timeout=10)
    loop: asyncio.AbstractEventLoop = box["loop"]
    queue: asyncio.Queue = asyncio.run_coroutine_threadsafe(_connect(), loop).result(
        timeout=10
    )
    assert broker._loops[id(queue)] is loop  # noqa: SLF001 - white-box check

    # Kill the subscriber's loop, then broadcast from this thread: no
    # exception, the dead queue is untracked, and nothing was enqueued.
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=10)
    loop.close()  # stopped is not enough: call_soon_threadsafe only raises once closed
    assert loop.is_closed()
    broker.broadcast({"type": "ping"})
    assert queue.qsize() == 0
    assert len(broker._queues) == 0  # noqa: SLF001 - white-box check
    assert id(queue) not in broker._loops  # noqa: SLF001 - white-box check


# ── M8: bounded queues and subscriber cap ─────────────────────────────────


@pytest.mark.parametrize("broker_cls", _BROKERS)
def test_broker_queues_are_bounded(broker_cls) -> None:
    broker = broker_cls()
    queue = broker.connect()
    assert queue.maxsize == 1000
    # Slow client: the queue fills, further events are dropped, never block.
    for _ in range(1500):
        broker.broadcast({"type": "fill"})
    assert queue.qsize() == 1000


def test_sse_broker_subscriber_cap() -> None:
    broker = _SSEBroker()
    for _ in range(300):
        broker.connect()
    assert len(broker._queues) == 256  # noqa: SLF001


def test_activity_broker_subscriber_cap() -> None:
    broker = _ActivityStreamBroker()
    for _ in range(300):
        broker.connect()
    assert broker.connected_count == 256


# ── M5: worker --loop stage isolation ─────────────────────────────────────


class _StopLoop(Exception):
    """Sentinel to break the worker's infinite loop in tests."""


def test_worker_loop_isolates_stage_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    import ingestion.worker as worker_mod

    monkeypatch.setattr(sys, "argv", ["worker", "--loop"])
    monkeypatch.setattr("storage.database.run_migrations", lambda: None)
    monkeypatch.setattr(
        "storage.database.acquire_sqlite_writer_lock", lambda settings: 42
    )
    monkeypatch.setattr(worker_mod, "ensure_background_probe", lambda: None)

    calls = {"run_once": 0, "forecast": 0, "retention": 0, "parity": 0, "drift": 0}

    def run_once():
        calls["run_once"] += 1
        if calls["run_once"] == 1:
            raise RuntimeError("scan exploded")
        return {}

    def fail(name):
        def _fail():
            calls[name] += 1
            raise RuntimeError(f"{name} exploded")

        return _fail

    monkeypatch.setattr(worker_mod, "run_once", run_once)
    monkeypatch.setattr(worker_mod, "maybe_run_forecast", fail("forecast"))
    monkeypatch.setattr(worker_mod, "maybe_run_retention", fail("retention"))
    monkeypatch.setattr(worker_mod, "maybe_run_parity", fail("parity"))
    monkeypatch.setattr(worker_mod, "maybe_run_score_drift", fail("drift"))

    def fake_backoff(iteration, interval):
        if calls["run_once"] >= 2:
            raise _StopLoop()
        return 0

    monkeypatch.setattr(worker_mod, "backoff_sleep_seconds", fake_backoff)

    with pytest.raises(_StopLoop):
        worker_mod.main()

    # Every stage was attempted on both iterations despite the failures —
    # no single stage killed the loop.
    assert calls == {
        "run_once": 2,
        "forecast": 2,
        "retention": 2,
        "parity": 2,
        "drift": 2,
    }


# ── M6: stop-aware watchdog phases ────────────────────────────────────────


def test_watchdog_phase_abandoned_on_shutdown() -> None:
    """SIGINT during a wedged phase returns promptly instead of waiting out the timeout."""
    from engine.run import _run_watchdog_phase

    stop = threading.Event()
    started = threading.Event()

    def slow_fn():
        started.set()
        time.sleep(30)
        return {"ok": True}

    holder: dict = {}
    thread = threading.Thread(
        target=lambda: holder.setdefault(
            "outcome",
            _run_watchdog_phase(
                stage="m6-shutdown-test",
                component="m6",
                timeout_seconds=30,
                fn=slow_fn,
                stop=stop,
            ),
        ),
        daemon=True,
    )
    thread.start()
    assert started.wait(timeout=10)
    begin = time.monotonic()
    stop.set()
    thread.join(timeout=10)
    elapsed = time.monotonic() - begin
    assert not thread.is_alive()
    assert holder["outcome"] is None
    assert elapsed < 5, f"shutdown blocked {elapsed:.1f}s on a wedged phase"


def test_watchdog_phase_result_propagates_without_stop() -> None:
    from engine.run import _run_watchdog_phase

    outcome = _run_watchdog_phase(
        stage="m6-ok-test",
        component="m6",
        timeout_seconds=5,
        fn=lambda: {"ok": True},
        stop=threading.Event(),
    )
    assert outcome is not None
    assert outcome.result == {"ok": True}


def test_watchdog_phase_exception_propagates_without_stop() -> None:
    from engine.run import _run_watchdog_phase

    def boom():
        raise RuntimeError("phase exploded")

    with pytest.raises(RuntimeError, match="phase exploded"):
        _run_watchdog_phase(
            stage="m6-err-test",
            component="m6",
            timeout_seconds=5,
            fn=boom,
            stop=threading.Event(),
        )


def test_watchdog_phase_skipped_when_stop_preset() -> None:
    """A stop set before the phase starts means no new phase work begins."""
    from engine.run import _run_watchdog_phase

    stop = threading.Event()
    stop.set()
    started = threading.Event()

    def slow_fn():
        started.set()
        return {}

    outcome = _run_watchdog_phase(
        stage="m6-preset-test",
        component="m6",
        timeout_seconds=5,
        fn=slow_fn,
        stop=stop,
    )
    assert outcome is None
    assert not started.is_set()


# ── L2: None-returning phase is a completion, not a wedge skip ─────────────


def test_watchdog_phase_none_result_is_completion_not_skip() -> None:
    """A phase fn returning None violates the dict-return contract, but the
    run still completed: it must not be misclassified as a wedge skip (which
    would accumulate skip counters and fire false red re-alerts)."""
    from engine.run import _run_watchdog_phase

    def none_fn():  # noqa: ANN202 - deliberately violates the dict contract
        return None

    outcome = _run_watchdog_phase(
        stage="l2-none-test",
        component="l2",
        timeout_seconds=5,
        fn=none_fn,  # type: ignore[arg-type]
        stop=None,
    )
    assert outcome is not None
    assert outcome.timed_out is False
    assert outcome.skipped is False
    assert outcome.result is None
    # A second identical run must also complete, never skip on a phantom wedge.
    again = _run_watchdog_phase(
        stage="l2-none-test",
        component="l2",
        timeout_seconds=5,
        fn=none_fn,  # type: ignore[arg-type]
        stop=None,
    )
    assert again is not None
    assert again.skipped is False
