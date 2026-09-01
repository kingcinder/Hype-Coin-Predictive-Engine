from __future__ import annotations

import threading
import time

from sqlalchemy import select

from common.config import Settings
from engine.run import run_engine_phases
from storage import models

# None of these health components is written by any other engine path, so the
# assertion "each contributes exactly one red row" is unambiguous here.
WATCHDOG_COMPONENTS = ["forecast", "lake", "parity", "nightcrawler", "data_lake"]


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


def _phase_settings() -> Settings:
    """Tiny watchdog timeouts so blocking stubs abandon fast; nightcrawler +
    data-lake enabled so every phase actually runs. Explicit kwargs win over
    any of the project's ``.env`` values."""
    return Settings(
        nightcrawler_enabled=True,
        nightcrawler_interval_minutes=1,
        data_lake_enabled=True,
        forecast_timeout_seconds=0.05,
        retention_timeout_seconds=0.05,
        parity_timeout_seconds=0.05,
        nightcrawler_timeout_seconds=0.05,
        data_lake_timeout_seconds=0.05,
    )


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
    )
    elapsed = time.monotonic() - started

    assert phase_error is True
    assert elapsed < 5.0  # returned promptly, never waited out the wedged fns
    # Each guarded phase is marked errored (parity is informational, so only the
    # four that gate completion should have been marked).
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
    assert len(rows) == len(WATCHDOG_COMPONENTS) == 5
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
    )
    # Skips are transient, not failures, and no phase completed the night-crawl.
    assert second_error is False
    assert second_last_nc == 0.0
    # Still exactly 5 red alarms — the skips recorded no new rows.
    assert len(_red_rows(session)) == 5
