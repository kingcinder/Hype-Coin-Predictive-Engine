"""Harvest paths: the executable episode behind the Task 7 labels.

A price peak you cannot sell into is not a peak. Every candidate sell
is sized at a reference fraction of the episode's PEAK quote depth —
``N(f) = f * D_peak``, never of entry depth — and filled against the
row's constant-product pool, realizing the average fill price
``price * D / (D + N)``. Executable value is therefore a price-like
series per reference fraction, and its peak can sit before the spot
peak when the depth was there and not at the thin top print. All exit
labels (Task 8's grading included) are computed from this series,
never from spot prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from common.config import Settings
from common.time import ensure_utc

REFERENCE_DEPTH_FRACTIONS: tuple[float, ...] = (0.02, 0.10, 0.25)

# The evaluator's reference size: the harvest window and the second-leg
# test are computed on the 10% executable-value series, the same size
# Task 8 grades captured fraction against.
WINDOW_REFERENCE_FRACTION = 0.10

# A collapse crossing is negated only by a recovery inside this window.
_COLLAPSE_RECOVERY_WINDOW = timedelta(hours=4)


@dataclass(frozen=True)
class HarvestPath:
    executable_peak_gain_pct: dict[float, float]  # keyed by reference fraction
    harvest_window_start_hours: float | None  # None when no row qualifies
    collapse_confirmed: bool
    second_leg: bool


def _exec_price(price: float, depth: float, notional: float) -> float:
    """Average fill price for selling ``notional`` USD into a pool whose
    quote depth is ``depth`` USD at spot ``price`` (constant product)."""
    return price * depth / (depth + notional)


def harvest_path(
    entry_price: float,
    start: datetime,
    rows: list[tuple[datetime, float, float | None]],
    *,
    settings: Settings,
) -> HarvestPath | None:
    """The executable episode over one forward window.

    ``rows`` are ``(ts, price, quote_depth_usd)`` in time order, the
    depth at each row being the last depth print at or before its ts.
    Returns ``None`` when ``rows`` is empty or no row carries a usable
    depth (``None`` or ``<= 0``): without a depth path there is no
    executable episode to describe. Rows with unusable depth are
    skipped for executability but still count toward the peak price
    the collapse-confirmation drawdown is measured from; for the
    harvest window they fail the depth leg and break the interval.
    """
    if not rows:
        return None
    start_utc = ensure_utc(start)
    norm = [(ensure_utc(ts), float(price), depth) for ts, price, depth in rows]
    usable = [(ts, price, float(depth)) for ts, price, depth in norm if depth and depth > 0]
    if not usable:
        return None
    d_peak = max(depth for _, _, depth in usable)

    gains: dict[float, float] = {}
    for fraction in REFERENCE_DEPTH_FRACTIONS:
        notional = fraction * d_peak
        ev_peak = max(_exec_price(price, depth, notional) for _, price, depth in usable)
        gains[fraction] = ev_peak / entry_price - 1.0

    # The window and second-leg series, at the evaluator's reference size.
    ref_notional = WINDOW_REFERENCE_FRACTION * d_peak
    exec_ref: list[float | None] = [
        _exec_price(price, float(depth), ref_notional) if depth and depth > 0 else None
        for _, price, depth in norm
    ]
    window_start = _harvest_window_start(norm, exec_ref, d_peak, settings.harvest_theta, start_utc)
    return HarvestPath(
        executable_peak_gain_pct=gains,
        harvest_window_start_hours=window_start,
        collapse_confirmed=_collapse_confirmed(norm, settings),
        second_leg=_second_leg(exec_ref, settings),
    )


def _harvest_window_start(
    norm: list[tuple[datetime, float, float | None]],
    exec_ref: list[float | None],
    d_peak: float,
    theta: float,
    start_utc: datetime,
) -> float | None:
    """First row of the first maximal interval where EV is within
    ``theta`` of the executable peak AND depth holds >= 50% of peak."""
    ev_peak = max(ev for ev in exec_ref if ev is not None)
    ev_floor = (1.0 - theta) * ev_peak
    depth_floor = 0.50 * d_peak
    for (ts, _price, depth), ev in zip(norm, exec_ref, strict=True):
        if ev is not None and ev >= ev_floor and depth is not None and depth >= depth_floor:
            return (ts - start_utc).total_seconds() / 3600.0
    return None


def _collapse_confirmed(
    norm: list[tuple[datetime, float, float | None]],
    settings: Settings,
) -> bool:
    """First >= drawdown-fraction fall from the peak price whose
    following 4h hold no recovery to within the recovery fraction of
    the peak. A crossing that recovers inside 4h does not confirm; a
    later crossing that does not recover does."""
    peak_price = max(price for _, price, _ in norm)
    peak_index = next(i for i, (_, price, _) in enumerate(norm) if price == peak_price)
    cross_line = peak_price * (1.0 - settings.harvest_collapse_drawdown_fraction)
    recovery_line = peak_price * (1.0 - settings.harvest_collapse_recovery_fraction)
    for i in range(peak_index, len(norm)):
        ts, price, _ = norm[i]
        if price > cross_line:
            continue
        recovered = any(
            later_price >= recovery_line
            for later_ts, later_price, _ in norm[i + 1 :]
            if later_ts <= ts + _COLLAPSE_RECOVERY_WINDOW
        )
        if not recovered:
            return True
    return False


def _second_leg(exec_ref: list[float | None], settings: Settings) -> bool:
    """A new EV peak above the multiple of the prior EV peak, after a
    drawdown of at least the drawdown fraction from that prior peak."""
    drawdown_floor = 1.0 - settings.harvest_second_leg_drawdown_fraction
    running_peak: float | None = None
    leg_reference: float | None = None
    for ev in exec_ref:
        if ev is None:
            continue
        if running_peak is None:
            running_peak = ev
            continue
        if leg_reference is None:
            if ev > running_peak:
                running_peak = ev
            elif ev <= running_peak * drawdown_floor:
                leg_reference = running_peak
        elif ev > leg_reference * settings.harvest_second_leg_multiple:
            return True
        elif ev > leg_reference:
            # A higher peak that is not a second leg becomes the new
            # reference peak for any later drawdown.
            running_peak = ev
            leg_reference = None
    return False
