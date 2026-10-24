"""Regression tests for the engine phase watchdog hardening (REVIEW.md H3).

- Timing out a wedged stage abandons the thread, fires the ``on_timeout``
  hook, records the stage in ``abandoned_stages()`` / ``snapshot_phase_state()``,
  and skips re-running while the wedge is in flight.
- When the abandoned thread finally exits, its tracking is cleared so the
  next iteration runs the stage fresh.
- A normal (in-time) run is never marked abandoned and never fires the hook.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from ops.watchdog import (
    WatchdogState,
    abandoned_stages,
    check_consecutive_failures,
    check_disk_space,
    maybe_checkpoint_wal,
    maybe_vacuum,
    note_phase_skip,
    on_scan_failure,
    on_scan_success,
    reset_phase_skip,
    run_stage_with_timeout,
    run_watchdog,
    snapshot_phase_state,
)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_timeout_abandons_thread_fires_hook_and_skips_rerun() -> None:
    release = threading.Event()
    fired: list[str] = []

    def wedged() -> dict[str, object]:
        release.wait(timeout=10.0)
        return {"status": "ok"}

    outcome = run_stage_with_timeout(
        wedged,
        timeout_seconds=0.2,
        stage="watchdog-test-wedge",
        on_timeout=fired.append,
    )
    try:
        assert outcome.timed_out is True
        assert outcome.skipped is False
        assert fired == ["watchdog-test-wedge"]

        abandoned = abandoned_stages()
        assert "watchdog-test-wedge" in abandoned

        snap = {p["stage"]: p for p in snapshot_phase_state()}
        assert snap["watchdog-test-wedge"]["abandoned"] is True
        assert snap["watchdog-test-wedge"]["in_flight"] is True

        # While the wedge is still in flight, the stage is skipped — the
        # watchdog must not pile up a second daemon thread.
        calls: list[None] = []

        def should_not_run() -> dict[str, object]:
            calls.append(None)
            return {}

        second = run_stage_with_timeout(
            should_not_run, timeout_seconds=5.0, stage="watchdog-test-wedge"
        )
        assert second.skipped is True
        assert calls == []
    finally:
        release.set()

    # Once the abandoned thread exits, tracking clears and the stage runs fresh.
    assert _wait_until(lambda: "watchdog-test-wedge" not in abandoned_stages())
    fresh = run_stage_with_timeout(
        lambda: {"status": "ok"}, timeout_seconds=5.0, stage="watchdog-test-wedge"
    )
    assert fresh.timed_out is False
    assert fresh.skipped is False
    assert fresh.result == {"status": "ok"}
    assert "watchdog-test-wedge" not in abandoned_stages()


def test_normal_run_is_never_abandoned_and_fires_no_hook() -> None:
    fired: list[str] = []
    outcome = run_stage_with_timeout(
        lambda: {"status": "ok"},
        timeout_seconds=5.0,
        stage="watchdog-test-clean",
        on_timeout=fired.append,
    )
    assert outcome.timed_out is False
    assert outcome.skipped is False
    assert outcome.result == {"status": "ok"}
    assert fired == []
    assert "watchdog-test-clean" not in abandoned_stages()
    assert "watchdog-test-clean" not in {p["stage"] for p in snapshot_phase_state()}


def test_timeout_hook_failure_does_not_escape() -> None:
    release = threading.Event()

    def broken_hook(_stage: str) -> None:
        raise RuntimeError("notification transport failed")

    outcome = run_stage_with_timeout(
        lambda: release.wait(timeout=5.0) or {"status": "ok"},
        timeout_seconds=0.01,
        stage="watchdog-hook-failure",
        on_timeout=broken_hook,
    )
    assert outcome.timed_out is True
    assert outcome.skipped is False
    release.set()
    assert _wait_until(lambda: "watchdog-hook-failure" not in abandoned_stages())


def test_skip_alerts_obey_count_cap_and_reset_for_recovery(monkeypatch) -> None:
    import ops.watchdog as watchdog

    now = [100.0]
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: now[0])

    assert note_phase_skip("capped-stage", threshold=2, max_alerts=1) == "counted"
    assert note_phase_skip("capped-stage", threshold=2, max_alerts=1) == "alert"
    assert note_phase_skip("capped-stage", threshold=2, max_alerts=1) == "counted"
    assert note_phase_skip("capped-stage", threshold=2, max_alerts=1) == "muted_alerts"
    reset_phase_skip("capped-stage")
    assert note_phase_skip("capped-stage", threshold=1, max_alerts=1) == "alert"


def test_skip_alert_duration_cap_mutes_elapsed_wedge(monkeypatch) -> None:
    import ops.watchdog as watchdog

    now = [100.0]
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: now[0])
    assert note_phase_skip("aged-stage", threshold=1, max_duration_seconds=5.0) == "alert"
    now[0] = 105.0
    assert note_phase_skip("aged-stage", threshold=1, max_duration_seconds=5.0) == "muted_duration"


def test_run_watchdog_reports_degraded_after_failure_threshold(monkeypatch) -> None:
    import ops.watchdog as watchdog

    monkeypatch.setattr(watchdog, "maybe_checkpoint_wal", lambda: None)
    monkeypatch.setattr(watchdog, "maybe_vacuum", lambda _state: None)
    monkeypatch.setattr(watchdog, "check_disk_space", lambda _session: {"database_pct": 12.0})
    state = WatchdogState(consecutive_scan_failures=5, total_scan_failures=5)

    result = run_watchdog(object(), state)

    assert result["iteration"] == 1
    assert result["database_pct"] == 12.0
    assert result["degraded"] is True
    assert state.total_iterations == 1


def test_scan_failure_counter_resets_on_recovery() -> None:
    from ops.watchdog import WatchdogState

    state = WatchdogState()
    on_scan_failure(state)
    on_scan_failure(state)
    assert state.consecutive_scan_failures == 2
    assert state.total_scan_failures == 2
    assert check_consecutive_failures(state) is False

    on_scan_success(state)
    assert state.consecutive_scan_failures == 0
    assert state.total_scan_failures == 2


def test_sqlite_checkpoint_and_vacuum_run_and_advance_schedule(monkeypatch) -> None:
    import ops.watchdog as watchdog
    import storage.database

    executed: list[str] = []
    commits: list[bool] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def execute(self, statement):
            executed.append(str(statement))

        def commit(self):
            commits.append(True)

    monkeypatch.setattr(
        watchdog, "get_settings", lambda: SimpleNamespace(database_url="sqlite:///engine.db")
    )
    monkeypatch.setattr(storage.database, "SessionLocal", FakeSession)
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 22_000.0)
    state = WatchdogState(last_vacuum_monotonic=0.0)

    maybe_checkpoint_wal()
    maybe_vacuum(state)

    assert executed == ["PRAGMA wal_checkpoint(TRUNCATE)", "VACUUM"]
    assert len(commits) == 2
    assert state.last_vacuum_monotonic == 22_000.0


def test_disk_space_check_records_warning_and_critical_states(session, monkeypatch) -> None:
    import ops.watchdog as watchdog
    from storage.models import SystemHealth

    settings = SimpleNamespace(
        database_url="sqlite:///tmp/engine.db", archive_local_dir="/archive/data"
    )
    monkeypatch.setattr(watchdog, "get_settings", lambda: settings)

    def set_used_pct(pct: int) -> None:
        stat = SimpleNamespace(f_blocks=100, f_frsize=1, f_bavail=100 - pct)
        monkeypatch.setattr(watchdog.os, "statvfs", lambda _path: stat)

    set_used_pct(86)
    warning = check_disk_space(session)
    assert warning["database_pct"] == 86.0
    assert session.query(SystemHealth).filter_by(component="disk").one().state == "yellow"

    set_used_pct(96)
    critical = check_disk_space(session)
    assert critical["archive_pct"] == 96.0
    disk_rows = session.query(SystemHealth).filter_by(component="disk").all()
    assert disk_rows[-1].state == "red"
    assert disk_rows[-1].error_count == 1


def test_disk_space_check_skips_unreadable_paths_and_non_session_health(monkeypatch) -> None:
    import ops.watchdog as watchdog

    monkeypatch.setattr(
        watchdog,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="sqlite:///tmp/engine.db", archive_local_dir="/archive"
        ),
    )
    calls = iter([OSError("missing mount"), SimpleNamespace(f_blocks=0, f_frsize=1, f_bavail=0)])

    def statvfs(_path):
        value = next(calls)
        if isinstance(value, OSError):
            raise value
        return value

    monkeypatch.setattr(watchdog.os, "statvfs", statvfs)

    result = check_disk_space(object())

    assert result["archive_pct"] == 0.0
    assert "database_pct" not in result


def test_run_watchdog_persists_health_for_database_session(session, monkeypatch) -> None:
    import ops.watchdog as watchdog
    from storage.models import SystemHealth

    monkeypatch.setattr(watchdog, "maybe_checkpoint_wal", lambda: None)
    monkeypatch.setattr(watchdog, "maybe_vacuum", lambda _state: None)
    monkeypatch.setattr(watchdog, "check_disk_space", lambda _session: {})
    state = WatchdogState(consecutive_scan_failures=1, total_scan_failures=3)

    result = run_watchdog(session, state)

    assert result["degraded"] is False
    health = session.query(SystemHealth).filter_by(component="watchdog").one()
    assert health.state == "yellow"
    assert health.error_count == 1


def test_stage_exception_is_reraised_in_calling_thread() -> None:
    import pytest

    with pytest.raises(ValueError, match="stage failed"):
        run_stage_with_timeout(
            lambda: (_ for _ in ()).throw(ValueError("stage failed")),
            timeout_seconds=5.0,
            stage="watchdog-stage-error",
        )
