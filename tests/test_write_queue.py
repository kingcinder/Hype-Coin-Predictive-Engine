"""Stress tests for the SQLite write serialization queue (E5 / item #6).

Covers the done criteria:

- zero ``"database is locked"`` errors under concurrent write load,
- write (execution) latency p99 < 100ms,
- clean shutdown drains the queue (no lost writes).

Plus: WAL mode actually engaged, data integrity through the queue, failed
tasks don't kill the writer, re-entrant submit from the writer thread runs
inline, ``record_health`` routing through the queue, and the
``WriteQueueNotRunning`` guard rails.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import func, select, text

from common.time import utc_now
from storage import models
from storage.database import Base, make_engine
from storage.repository import (
    get_or_create_chain,
    queued_write,
    record_health,
    upsert_asset,
)
from storage.write_queue import (
    WriteQueue,
    WriteQueueNotRunning,
    get_write_queue,
    start_write_queue,
    stop_write_queue,
)

WRITERS = 8
WRITES_PER_THREAD = 25
READERS = 2


@pytest.fixture()
def session_factory(tmp_path):
    """File-backed SQLite DB wired exactly like production (WAL pragma etc.)."""
    engine = make_engine(f"sqlite:///{tmp_path}/wq_test.db")
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture()
def queue(session_factory):
    """A started WriteQueue, always stopped (drained) on teardown."""
    q = WriteQueue(
        session_factory=session_factory,
        heartbeat_interval_sec=3600.0,
        heartbeat_every_n_writes=10**9,
    )
    q.start()
    yield q
    q.stop(timeout=30)


@pytest.fixture(autouse=True)
def _no_leftover_singleton():
    yield
    stop_write_queue(timeout=30)


def _health_write(session, component: str) -> int:
    row = record_health(session, component=component, state="ok", message=component)
    return row.id  # attribute read on the writer thread — safe


# ----------------------------------------------------------------------
# Done criteria
# ----------------------------------------------------------------------


def test_wal_mode_engaged(queue, session_factory):
    with session_factory() as session:
        mode = session.execute(text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "wal"


def test_concurrent_writes_no_lock_errors(queue, session_factory):
    """8 writers x 25 serialized writes + 2 concurrent readers.

    Asserts zero "database is locked" errors, exact row counts, and
    execution-latency p99 < 100ms (queue-wait is reported separately —
    it is backpressure, visible via queue_depth, not DB latency).
    """
    total = WRITERS * WRITES_PER_THREAD
    barrier = threading.Barrier(WRITERS + READERS)
    stop_readers = threading.Event()
    errors: list[BaseException] = []
    end_to_end: list[float] = []
    guard = threading.Lock()

    def writer(tid: int) -> None:
        barrier.wait()
        for i in range(WRITES_PER_THREAD):
            t0 = time.monotonic()
            try:
                queue.submit_sync(lambda s, tid=tid, i=i: _health_write(s, f"stress-{tid}-{i}"))
            except Exception as exc:  # noqa: BLE001
                with guard:
                    errors.append(exc)
            else:
                with guard:
                    end_to_end.append(time.monotonic() - t0)

    def reader() -> None:
        barrier.wait()
        while not stop_readers.is_set():
            with session_factory() as session:
                session.execute(
                    select(func.count())
                    .select_from(models.SystemHealth)
                    .where(models.SystemHealth.component.like("stress-%"))
                ).scalar()

    threads = [
        threading.Thread(target=writer, args=(t,), name=f"wq-writer-{t}") for t in range(WRITERS)
    ] + [threading.Thread(target=reader, name=f"wq-reader-{r}") for r in range(READERS)]
    for t in threads:
        t.start()
    for t in threads[:WRITERS]:
        t.join(timeout=120)
    stop_readers.set()
    for t in threads[WRITERS:]:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "test threads did not finish"
    assert errors == [], f"{len(errors)} write errors: {errors[:3]}"
    assert len(end_to_end) == total

    with session_factory() as session:
        n = session.execute(
            select(func.count())
            .select_from(models.SystemHealth)
            .where(models.SystemHealth.component.like("stress-%"))
        ).scalar()
    assert n == total, f"lost writes: {n} rows for {total} submitted"

    stats = queue.stats()
    exec_p99_ms = stats["exec_latency_p99_sec"] * 1000
    wait_p99_ms = stats["queue_wait_p99_sec"] * 1000
    e2e_p99_ms = sorted(end_to_end)[int(0.99 * (len(end_to_end) - 1))] * 1000
    print(
        f"\n[write_queue] exec_p99={exec_p99_ms:.1f}ms "
        f"queue_wait_p99={wait_p99_ms:.1f}ms end_to_end_p99={e2e_p99_ms:.1f}ms "
        f"depth_max_observed~{stats['queue_depth']}"
    )
    assert stats["writes_failed"] == 0
    assert exec_p99_ms < 100.0, f"write execution p99 {exec_p99_ms:.1f}ms >= 100ms"


def test_clean_shutdown_drains_queue(session_factory):
    q = WriteQueue(
        session_factory=session_factory,
        heartbeat_interval_sec=3600.0,
        heartbeat_every_n_writes=10**9,
    )
    q.start()

    def slow_write(session, i: int) -> int:
        time.sleep(0.01)
        return _health_write(session, f"drain-{i}")

    futures = [q.submit(lambda s, i=i: slow_write(s, i)) for i in range(60)]
    # Stop immediately — every accepted write must still commit.
    assert q.stop(timeout=30) is True
    assert all(f.done() for f in futures)
    assert [f.result(timeout=5) for f in futures]  # re-raises if any failed

    with session_factory() as session:
        n = session.execute(
            select(func.count())
            .select_from(models.SystemHealth)
            .where(models.SystemHealth.component.like("drain-%"))
        ).scalar()
    assert n == 60, f"shutdown lost writes: {n}/60 rows present"


# ----------------------------------------------------------------------
# Behavior / integration
# ----------------------------------------------------------------------


def test_queued_writes_data_integrity(queue, session_factory):
    chain_id = queue.submit_sync(
        lambda s: (
            get_or_create_chain(
                s, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
            ).id
        )
    )
    asset_ids = [
        queue.submit_sync(
            lambda s, i=i: (
                upsert_asset(
                    s,
                    chain_id=chain_id,
                    address=f"Token{i:040d}",
                    symbol=f"T{i}",
                    name=f"Token {i}",
                    first_seen_at=utc_now(),
                ).id
            )
        )
        for i in range(20)
    ]
    assert len(set(asset_ids)) == 20

    with session_factory() as session:
        rows = session.scalars(select(models.Asset).where(models.Asset.chain_id == chain_id)).all()
    assert len(rows) == 20
    by_symbol = {r.symbol: r for r in rows}
    assert by_symbol["T5"].address == f"Token{5:040d}"
    assert by_symbol["T19"].name == "Token 19"


def test_failed_task_does_not_kill_writer(queue):
    fut = queue.submit(lambda s: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        fut.result(timeout=10)
    assert queue.submit_sync(lambda s: "alive", timeout=10) == "alive"
    assert queue.stats()["writes_failed"] == 1


def test_reentrant_submit_runs_inline(queue):
    def outer(session):
        assert queue.is_writer_thread()
        return queue.submit(lambda s2: 41 + 1).result(timeout=10)

    assert queue.submit_sync(outer, timeout=10) == 42


def test_heartbeat_persists_system_health(session_factory):
    q = WriteQueue(
        session_factory=session_factory,
        heartbeat_interval_sec=3600.0,
        heartbeat_every_n_writes=5,
    )
    q.start()
    try:
        for i in range(5):
            q.submit_sync(lambda s, i=i: _health_write(s, f"hb-{i}"), timeout=10)
    finally:
        assert q.stop(timeout=30) is True

    with session_factory() as session:
        rows = session.scalars(
            select(models.SystemHealth).where(models.SystemHealth.component == "write_queue")
        ).all()
    assert len(rows) >= 1
    assert "writes=" in (rows[-1].message or "")
    assert rows[-1].state in ("ok", "yellow")


def test_record_health_routes_through_queue(session_factory):
    start_write_queue(session_factory=session_factory)
    try:
        with session_factory() as session:
            # Routed fire-and-forget through the queue: returns None.
            assert (
                record_health(session, component="routed-probe", state="ok", message="via queue")
                is None
            )
    finally:
        assert stop_write_queue(timeout=30) is True  # drains the routed write

    with session_factory() as session:
        row = session.scalar(
            select(models.SystemHealth).where(models.SystemHealth.component == "routed-probe")
        )
    assert row is not None and row.state == "ok"

    # With no queue running, the classic direct behavior is restored.
    assert get_write_queue() is None
    with session_factory() as session:
        row = record_health(session, component="direct-probe", state="ok")
        assert row is not None
        session.commit()


def test_queued_write_uses_singleton(session_factory):
    start_write_queue(session_factory=session_factory)
    try:
        assert queued_write(lambda s: 2 + 2, timeout=10) == 4
    finally:
        stop_write_queue(timeout=30)


def test_submit_without_running_queue_raises():
    q = WriteQueue(session_factory=lambda: None)  # never started
    with pytest.raises(WriteQueueNotRunning):
        q.submit(lambda s: None)
    with pytest.raises(WriteQueueNotRunning):
        queued_write(lambda s: None)
