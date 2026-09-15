"""Point-in-time leakage guardrails (fabrication plan item #8).

Automated enforcement that no data observed *after* the decision time can
flow into feature computation. The engine's mission promises scans that are
replayable "without future leakage"; ``docs/leakage-audit.md`` verified the
per-feature query filters, and this module turns that invariant into code
that fails a build instead of silently consuming leaked data.

Three layers:

1. :func:`frozen_decision_ts` — a context manager that freezes one
   ``decision_ts`` for a feature-build scope (one scan, one backtest step).
   A guarded entry point that receives a different or missing
   ``decision_ts`` inside the frozen scope raises :class:`LeakageViolation`.
2. :func:`require_decision_ts` — a decorator for feature entry points
   (``FeatureFactory.build_for_asset`` / ``persist_for_assets``,
   ``LakeFeatureFactory.build_for_asset(s)``). It makes ``decision_ts`` a
   hard requirement instead of a convention: ``None``/missing raises, and a
   value that disagrees with the frozen scope raises.
3. :func:`check_rows_point_in_time` — asserts every input row's
   ``observed_at`` (the "when it became knowable" timestamp) is at or before
   the decision time. This is the defensive second net behind the SQL
   filters (``observed_at <= decision_ts``): if a future row ever reaches
   feature math, the build fails loudly instead of leaking.

Violations are logged to ``SystemHealth`` via
:func:`record_leakage_violation` (component ``leakage_guard``) so leaks are
visible in the same health surface as every other engine component.
"""

from __future__ import annotations

import contextvars
import inspect
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from typing import Any

from common.logging import get_logger
from common.time import ensure_utc

log = get_logger(__name__)

__all__ = [
    "LeakageViolation",
    "check_rows_point_in_time",
    "current_decision_ts",
    "frozen_decision_ts",
    "record_leakage_violation",
    "require_decision_ts",
]

#: SystemHealth component name used for every recorded leakage violation.
LEAKAGE_GUARD_COMPONENT = "leakage_guard"


class LeakageViolation(Exception):
    """A feature build tried to use data observed after its decision time."""


# The decision time frozen for the current build scope. ``contextvars`` keeps
# parallel backtest workers from stepping on each other's frozen value.
_frozen_decision_ts: contextvars.ContextVar[datetime | None] = contextvars.ContextVar(
    "leakage_guard_decision_ts", default=None
)


@contextmanager
def frozen_decision_ts(decision_ts: datetime):
    """Freeze ``decision_ts`` for the enclosed feature-build scope.

    Yields the frozen (UTC-normalized) timestamp. Guarded entry points called
    inside the scope must carry exactly this value — anything else raises
    :class:`LeakageViolation`.
    """
    frozen = ensure_utc(decision_ts)
    token = _frozen_decision_ts.set(frozen)
    try:
        yield frozen
    finally:
        _frozen_decision_ts.reset(token)


def current_decision_ts() -> datetime | None:
    """The decision time frozen for the current scope, if any."""
    return _frozen_decision_ts.get()


def require_decision_ts[F: Callable[..., Any]](fn: F) -> F:
    """Decorator: make ``decision_ts`` a hard requirement of a feature entry point.

    The wrapped callable must name a ``decision_ts`` parameter. Calling it
    with ``None`` (or omitting the argument) raises :class:`LeakageViolation`.
    If a :func:`frozen_decision_ts` scope is active, the value must also match
    the frozen timestamp exactly.
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            bound = inspect.signature(fn).bind(*args, **kwargs)
        except TypeError as exc:
            raise LeakageViolation(
                f"leakage guard: {fn.__qualname__} could not bind its decision_ts parameter: {exc}"
            ) from exc
        bound.apply_defaults()
        decision_ts = bound.arguments.get("decision_ts")
        if decision_ts is None:
            raise LeakageViolation(
                f"leakage guard: {fn.__qualname__} requires a decision_ts — "
                "feature computation without a frozen point-in-time is not allowed"
            )
        frozen = _frozen_decision_ts.get()
        if frozen is not None and ensure_utc(decision_ts) != frozen:
            raise LeakageViolation(
                f"leakage guard: {fn.__qualname__} decision_ts="
                f"{ensure_utc(decision_ts).isoformat()} does not match the frozen "
                f"scope decision_ts={frozen.isoformat()}"
            )
        return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def _row_timestamp(row: Any, ts_attr: str) -> datetime | None:
    """Read a timestamp off an ORM row, SimpleNamespace, mapping, or tuple."""
    if isinstance(row, dict):
        value = row.get(ts_attr)
    elif isinstance(row, (tuple, list)) and ts_attr in ("observed_at", "ts"):
        # DuckDB-style positional rows: (observed_at, ..., ts, ...) — not the
        # shapes feature code passes, but fail safe instead of guessing.
        return None
    else:
        value = getattr(row, ts_attr, None)
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    return None


def check_rows_point_in_time(
    rows: Iterable[Any],
    decision_ts: datetime,
    *,
    ts_attr: str = "observed_at",
    feature_name: str = "",
    max_reported: int = 5,
) -> int:
    """Assert every row in ``rows`` was observed at or before ``decision_ts``.

    ``ts_attr`` names the "when it became knowable" column — ``observed_at``
    by default, matching the audit's invariant. Rows without the attribute
    are skipped (they carry no timestamp to leak).

    Returns the number of rows checked. Raises :class:`LeakageViolation`
    naming the first offending rows when any row's timestamp is newer than
    the decision time.
    """
    cutoff = ensure_utc(decision_ts)
    checked = 0
    offenders: list[str] = []
    for index, row in enumerate(rows):
        ts = _row_timestamp(row, ts_attr)
        if ts is None:
            continue
        checked += 1
        if ts > cutoff:
            offenders.append(f"row[{index}].{ts_attr}={ts.isoformat()}")
    if offenders:
        shown = ", ".join(offenders[:max_reported])
        more = f" (+{len(offenders) - max_reported} more)" if len(offenders) > max_reported else ""
        label = f" feature={feature_name!r}" if feature_name else ""
        raise LeakageViolation(
            f"leakage guard: {len(offenders)} row(s) with {ts_attr} newer than "
            f"decision_ts={cutoff.isoformat()}{label}: {shown}{more}"
        )
    return checked


def record_leakage_violation(
    session: Any,
    *,
    feature_name: str = "",
    decision_ts: datetime | None = None,
    detail: str = "",
    component: str = LEAKAGE_GUARD_COMPONENT,
) -> None:
    """Log a leakage violation to ``SystemHealth`` (component ``leakage_guard``).

    Fail-safe: a logging failure must never mask the original violation, so
    errors here are logged and swallowed.
    """
    try:
        from storage.repository import (  # noqa: PLC0415 - lazy, keeps validation import-light
            record_health,
        )

        message = "point-in-time leakage violation"
        if feature_name:
            message += f" in {feature_name}"
        if decision_ts is not None:
            message += f" (decision_ts={ensure_utc(decision_ts).isoformat()})"
        if detail:
            message += f": {detail}"
        record_health(
            session,
            component=component,
            state="violation",
            error_count=1,
            message=message,
        )
        log.error("leakage guard violation recorded to SystemHealth: %s", message)
    except Exception:  # noqa: BLE001 - logging must never mask the violation
        log.exception("leakage guard: failed to record violation to SystemHealth")
