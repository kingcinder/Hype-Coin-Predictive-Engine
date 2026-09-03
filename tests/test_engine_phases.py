from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any

from sqlalchemy import select

from common.config import Settings
from engine.run import run_engine_phases
from storage import models

# None of these health components is written by any other engine path, so the
# assertion "each contributes exactly one red row" is unambiguous here.
WATCHDOG_COMPONENTS = ["forecast", "lake", "parity", "nightcrawler", "data_lake", "score_drift"]


class _FakeEngineState:
    """Hermetic stand-in for the global engine_state singleton: records the
    mark_* calls so we can assert the loop progressed without touching SSE."""

    def __init__(self) -> None:
        self.forecasting = 0
        self.retention = 0
        self.errors: list[str] = []

    def mark_forecasting(self) -> None:
        self.forecasting += 1

    def mark_retention(self) -> None:
        self.retention += 1

    def mark_error(self, message: str) -> None:
        self.errors.append(message)


class _LogRecorder:
    """Stand-in for ``engine.run.log`` that records structured log calls so a
    test can assert on events (e.g. the skip log) without parsing structlog JSON."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str, event: str, **kw: Any) -> None:
        self.calls.append((level, event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self._record("info", event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._record("error", event, **kw)

    def exception(self, event: str, **kw: Any) -> None:
        self._record("exception", event, **kw)

    def debug(self, event: str, **kw: Any) -> None:
        self._record("debug", event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._record("warning", event, **kw)

    def critical(self, event: str, **kw: Any) -> None:
        self._record("critical", event, **kw)


def _phase_settings(**overrides: Any) -> Settings:
    """Tiny watchdog timeouts so blocking stubs abandon fast; nightcrawler +
    data-lake enabled so every phase actually runs. Explicit kwargs win over
    any of the project's ``.env`` values."""
    kwargs: dict[str, Any] = {
        "nightcrawler_enabled": True,
        "nightcrawler_interval_minutes": 1,
        "data_lake_enabled": True,
        "score_drift_enabled": True,
        "forecast_timeout_seconds": 0.05,
        "retention_timeout_seconds": 0.05,
        "parity_timeout_seconds": 0.05,
        "nightcrawler_timeout_seconds": 0.05,
        "data_lake_timeout_seconds": 0.05,
        "score_drift_timeout_seconds": 0.05,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _blocking(duration: float = 1.0):
    def _fn() -> dict[str, object]:
        time.sleep(duration)
        return {"status": "ok"}

    return _fn


def _red_rows(session) -> list[models.SystemHealth]:
    return list(
        session.scalars(
            select(models.SystemHealth).where(
                models.SystemHealth.component.in_(WATCHDOG_COMPONENTS)
            )
        )
    )


def test_wedged_phase_records_red_alarm_and_loop_continues(session) -> None:
    """Drive the engine loop's phase wiring with blocking stubs: every phase
    exceeds its watchdog deadline, so a red health alarm is recorded per phase
    and the iteration is flagged errored — but the loop keeps going (a follow-up
    iteration runs and skips the still-wedged phases instead of spawning new
    threads or failing again)."""
    settings = _phase_settings()
    state = _FakeEngineState()
    stop = threading.Event()

    # First iteration: every phase blocks past its deadline -> all time out.
    started = time.monotonic()
    phase_error, last_nc = run_engine_phases(
        settings=settings,
        iteration=1,
        stop=stop,
        last_nc_run_monotonic=0.0,
        system_state=state,
        alarm_session=session,
        forecast_fn=_blocking(),
        retention_fn=_blocking(),
        parity_fn=_blocking(),
        nightcrawler_fn=_blocking(),
        data_lake_fn=_blocking(),
        score_drift_fn=_blocking(),
    )
    elapsed = time.monotonic() - started

    assert phase_error is True
    assert elapsed < 5.0  # returned promptly, never waited out the wedged fns
    # Each guarded phase is marked errored (parity + score_drift are
    # informational, so only the four that gate completion should be marked).
    assert len(state.errors) == 4
    assert state.errors == [
        "forecast: watchdog timeout",
        "retention: watchdog timeout",
        "nightcrawler: watchdog timeout",
        "data_lake: watchdog timeout",
    ]
    # The interval-gated nightcrawler did not complete, so its timestamp did not
    # advance.
    assert last_nc == 0.0

    # Exactly one red watchdog alarm per phase, with the recognisable message.
    rows = _red_rows(session)
    assert len(rows) == len(WATCHDOG_COMPONENTS) == 6
    by_component = {r.component: r for r in rows}
    for component in WATCHDOG_COMPONENTS:
        row = by_component[component]
        assert row.state == "red"
        assert "watchdog timeout; pass abandoned, engine loop continuing" in (row.message or "")

    # Second iteration: the wedged daemon threads are still in flight, so every
    # phase is SKIPPED (not re-added to the alarm set, not errored again) — the
    # loop keeps running without piling up work or threading up.
    second_error, second_last_nc = run_engine_phases(
        settings=settings,
        iteration=2,
        stop=stop,
        last_nc_run_monotonic=0.0,
        system_state=_FakeEngineState(),
        alarm_session=session,
        forecast_fn=_blocking(),
        retention_fn=_blocking(),
        parity_fn=_blocking(),
        nightcrawler_fn=_blocking(),
        data_lake_fn=_blocking(),
        score_drift_fn=_blocking(),
    )
    # Skips are transient, not failures, and no phase completed the night-crawl.
    assert second_error is False
    assert second_last_nc == 0.0
    # Still exactly 6 red alarms — the skips recorded no new rows.
    assert len(_red_rows(session)) == 6


def test_repeatedly_stuck_phase_realerts_after_skip_alert_cycles(session) -> None:
    """A phase that stays wedged doesn't go silent for the whole wedge: after
    ``SKIP_ALERT_CYCLES`` consecutive skips it re-alerts with a fresh red
    watchdog row, so operators keep seeing the stuck phase surface."""
    settings = _phase_settings(skip_alert_cycles=2)
    state = _FakeEngineState()
    stop = threading.Event()
    # Wedged daemon threads stay in flight long enough to be skipped across all
    # three iterations (each sleeps well past the tiny watchdog deadline).
    stuck = _blocking(3.0)

    def _run(iteration: int) -> None:
        run_engine_phases(
            settings=settings,
            iteration=iteration,
            stop=stop,
            last_nc_run_monotonic=0.0,
            system_state=state,
            alarm_session=session,
            forecast_fn=stuck,
            retention_fn=stuck,
            parity_fn=stuck,
            nightcrawler_fn=stuck,
            data_lake_fn=stuck,
            score_drift_fn=stuck,
        )

    # Iteration 1: every phase times out -> one red alarm per phase.
    _run(1)
    assert len(_red_rows(session)) == len(WATCHDOG_COMPONENTS) == 6

    # Iteration 2: still wedged, every phase skips once. A single consecutive
    # skip is below the threshold of 2, so no new rows yet.
    _run(2)
    assert len(_red_rows(session)) == 6

    # Iteration 3: the second consecutive skip per phase reaches the threshold
    # -> every phase re-alerts, doubling the rows.
    _run(3)
    rows = _red_rows(session)
    assert len(rows) == 2 * len(WATCHDOG_COMPONENTS) == 12
    counts = Counter(r.component for r in rows)
    assert counts == {c: 2 for c in WATCHDOG_COMPONENTS}
    # Each component gained one *re-alert* row (distinct from the original
    # timeout), still matching the watchdog-alarm shape so it surfaces in the UI.
    for component in WATCHDOG_COMPONENTS:
        comp_rows = [r for r in rows if r.component == component]
        messages = [r.message or "" for r in comp_rows]
        assert any("still wedged after 2 consecutive skipped" in m for m in messages)
        assert all("watchdog timeout; pass abandoned" in m for m in messages)


def test_engine_state_snapshot_exposes_watchdog_phase_state() -> None:
    """The engine-state snapshot (fed to the SSE stream and ``engine_snapshot``
    in the GUI) carries the live per-phase watchdog state, so the UI can show
    which phase is currently wedged and that iterations are being skipped."""
    import threading

    from engine.state import engine_state
    from ops.watchdog import run_stage_with_timeout

    # Idle snapshot carries the (empty) watchdog section.
    idle = engine_state.snapshot()
    assert "watchdog" in idle
    assert idle["watchdog"] == {"phases": []}

    # Wedge a stage, then confirm the snapshot reflects it in real time.
    release = threading.Event()

    def _stuck() -> dict[str, object]:
        release.wait(timeout=5.0)
        return {"status": "ok"}

    try:
        out = run_stage_with_timeout(_stuck, timeout_seconds=0.02, stage="retention")
        assert out.timed_out is True
        by_stage = {p["stage"]: p for p in engine_state.snapshot()["watchdog"]["phases"]}
        assert by_stage["retention"]["in_flight"] is True
        assert by_stage["retention"]["consecutive_skips"] == 0
    finally:
        release.set()


def test_wedged_phase_full_lifecycle_timeout_skip_recover(session, monkeypatch) -> None:
    """Drive the engine loop through one wedged phase's full watchdog lifecycle:
    a red alarm on the first timeout, no alarm but a skip log on the next
    iteration (the abandoned run is still in flight), and recovery — no alarm,
    no error, the phase actually runs again — once the wedge clears."""
    import engine.run as engine_run
    from ops.watchdog import snapshot_phase_state

    recorder = _LogRecorder()
    monkeypatch.setattr(engine_run, "log", recorder)

    settings = _phase_settings()
    state = _FakeEngineState()
    stop = threading.Event()

    release = threading.Event()
    invocations: list[int] = []

    def retention_fn() -> dict[str, object]:
        invocations.append(1)
        release.wait(timeout=5.0)
        return {"status": "ok"}

    def quick() -> dict[str, object]:
        return {"status": "ok"}

    def _run(iteration: int) -> tuple[bool, list[tuple[str, str, dict[str, Any]]]]:
        logs_before = len(recorder.calls)
        phase_error, _ = run_engine_phases(
            settings=settings,
            iteration=iteration,
            stop=stop,
            last_nc_run_monotonic=0.0,
            system_state=state,
            alarm_session=session,
            forecast_fn=quick,
            retention_fn=retention_fn,
            parity_fn=quick,
            nightcrawler_fn=quick,
            data_lake_fn=quick,
            score_drift_fn=quick,
        )
        return phase_error, recorder.calls[logs_before:]

    def lake_reds() -> list[models.SystemHealth]:
        return [r for r in _red_rows(session) if r.component == "lake"]

    def skipped_log(
        logs: list[tuple[str, str, dict[str, Any]]],
    ) -> bool:
        return any(
            level == "info"
            and event == "engine_phase_skipped_still_wedged"
            and kw.get("stage") == "retention"
            for level, event, kw in logs
        )

    # ── Iteration 1: retention blocks past its deadline -> red lake alarm ──
    err1, logs1 = _run(1)
    assert err1 is True
    assert state.errors == ["retention: watchdog timeout"]
    reds = lake_reds()
    assert len(reds) == 1
    assert reds[0].state == "red"
    assert "watchdog timeout; pass abandoned, engine loop continuing" in (reds[0].message or "")
    assert any(
        level == "error" and event == "engine_stage_watchdog_timeout" for level, event, _ in logs1
    )

    # ── Iteration 2: still wedged -> skipped, no new alarm, no error ──
    err2, logs2 = _run(2)
    assert err2 is False
    assert len(lake_reds()) == 1  # the skip added no new alarm row
    assert skipped_log(logs2)

    # ── Recover: release the wedged run; wait for it to free the in-flight slot ──
    release.set()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        by_stage = {p["stage"]: p for p in snapshot_phase_state()}
        if not by_stage.get("retention", {}).get("in_flight", False):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("wedged retention thread did not clear after release")

    # ── Iteration 3: retention runs to completion -> recovery ──
    err3, logs3 = _run(3)
    assert err3 is False
    assert len(lake_reds()) == 1  # still no new alarm on recovery
    # The phase actually ran again (once for the original wedge, once now) — it
    # was not skipped.
    assert len(invocations) == 2
    assert not skipped_log(logs3)
    assert not any(
        level == "error" and event == "engine_stage_watchdog_timeout" for level, event, _ in logs3
    )
