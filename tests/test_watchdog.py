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

from ops.watchdog import (
    abandoned_stages,
    run_stage_with_timeout,
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
