"""Component-specific health freshness thresholds.

A component whose latest row is older than its own cadence cannot report a
*current* state: a red row from yesterday is history, not a live failure. The
API therefore labels such rows ``stale`` so Feed Health does not present an
abandoned feed as currently red/yellow.
"""

from __future__ import annotations

from datetime import datetime

from common.time import ensure_utc

STALE_STATE = "stale"
DEFAULT_STALE_AFTER_SECONDS = 6 * 3600.0

# Components that report every scan go stale quickly; components on an
# hourly/daily/weekly cadence get a matching window.
_EXACT_STALE_AFTER_SECONDS: dict[str, float] = {
    "worker": 30 * 60.0,
    "watchdog": 30 * 60.0,
    "write_queue": 30 * 60.0,
    "disk": 30 * 60.0,
    "lake": 6 * 3600.0,
    "lake_backlog": 6 * 3600.0,
    "lake_stale_warning": 48 * 3600.0,
    "data_lake": 2 * 3600.0,
    "parity": 7 * 24 * 3600.0,
    "score_drift": 6 * 3600.0,
    "outcome_labels": 6 * 3600.0,
    "risk_calibration": 7 * 24 * 3600.0,
    "risk_confidence_calibration": 7 * 24 * 3600.0,
    "confidence_band_calibration": 7 * 24 * 3600.0,
    "forecast_calibration": 7 * 24 * 3600.0,
    "forecast_drift": 7 * 24 * 3600.0,
    "nightcrawler_orchestrator": 12 * 3600.0,
    "nightcrawler_pipeline": 12 * 3600.0,
}
_PREFIX_STALE_AFTER_SECONDS: tuple[tuple[str, float], ...] = (
    ("source:", 60 * 60.0),
    ("rpc_pool:", 60 * 60.0),
    ("nightcrawler:", 12 * 3600.0),
)


def stale_after_seconds(component: str) -> float:
    """Freshness window for one component's latest health row."""
    exact = _EXACT_STALE_AFTER_SECONDS.get(component)
    if exact is not None:
        return exact
    for prefix, seconds in _PREFIX_STALE_AFTER_SECONDS:
        if component.startswith(prefix):
            return seconds
    return DEFAULT_STALE_AFTER_SECONDS


def aged_state(
    component: str,
    state: str,
    ts: datetime | None,
    *,
    now: datetime,
) -> str:
    """``stale`` when the row is older than the component's freshness window."""
    if ts is None:
        return state
    age = (ensure_utc(now) - ensure_utc(ts)).total_seconds()
    if age > stale_after_seconds(component):
        return STALE_STATE
    return state
