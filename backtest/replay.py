"""Causal point-in-time replay harness (mission-refactor Task 4).

``backtest.runner`` already replays causally for scoring backtests, but it
prices entries at the decision snapshot and reports binary hit/miss. This
module is the shared falsification instrument for the mission evaluators
(Tasks 5/6/8/9): it walks a grid of historical decision points, prices each
entry at the NEXT snapshot with market impact (``trading.costs.simulate_buy``),
and attaches the continuous forward path (``forecast.labels.forward_path``)
computed from the price actually paid.

Causality contract (pinned by ``tests/test_replay_harness.py``):

- Rows are loaded once per asset through
  ``backtest.runner.point_in_time_market_rows(..., decision_ts=end)``, which
  gates ``ts <= end AND observed_at <= end`` — a vendor-restated snapshot
  observed after ``end`` can never reach an entry or a path.
- The entry snapshot is the first with ``ts > decision_ts``.
- Forward rows are the priced snapshots in ``(entry_ts, decision_ts +
  window]`` — the same decision-anchored window the forward labels use, so
  ``ForwardPath.hours_to_peak`` keeps its "hours from decision" meaning.
- A decision point is skipped — never truncated into a fake label — when
  there is no entry snapshot, when ``entry_ts + window > end`` (the window
  is not fully observed inside the replay), or when no forward row exists
  to build a path from.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backtest.runner import point_in_time_market_rows
from common.time import ensure_utc
from forecast.labels import ForwardPath, forward_path
from trading.costs import simulate_buy


@dataclass(frozen=True)
class ReplayEntry:
    asset_id: int
    decision_ts: datetime
    entry_ts: datetime  # first snapshot strictly after decision_ts
    entry_price: float  # that snapshot's price, impact-adjusted via simulate_buy
    path: ForwardPath  # forward path from entry_price over the window
    # (ts, price) in (entry_ts, decision_ts + window]
    forward_rows: tuple[tuple[datetime, float], ...]


def iter_replay_entries(
    session: Session,
    *,
    asset_ids: list[int],
    start: datetime,
    end: datetime,
    step_hours: float = 1.0,
    forward_hours: int = 24,
    size_sol: Decimal = Decimal("1"),
    slip_pct: Decimal = Decimal("5"),
    fee_pct: Decimal = Decimal("1"),
    priority_sol: Decimal = Decimal("0"),
) -> Iterator[ReplayEntry]:
    """Yield one :class:`ReplayEntry` per (asset, grid decision) that earns one.

    Decision points sit on a ``step_hours`` grid from ``start`` to ``end``
    (inclusive). Per asset the snapshot series is loaded once and walked in
    memory — no per-step queries.
    """
    start = ensure_utc(start)
    end = ensure_utc(end)
    window = timedelta(hours=forward_hours)
    step = timedelta(hours=step_hours)
    if step <= timedelta(0):
        raise ValueError(f"step_hours must be positive, got {step_hours!r}")
    if window <= timedelta(0):
        raise ValueError(f"forward_hours must be positive, got {forward_hours!r}")

    for asset_id in asset_ids:
        series = _priced_series(session, asset_id=asset_id, end=end)
        stamps = [ts for ts, _price in series]
        decision = start
        while decision <= end:
            entry = _entry_at(
                series,
                stamps,
                asset_id=asset_id,
                decision_ts=decision,
                end=end,
                window=window,
                size_sol=size_sol,
                slip_pct=slip_pct,
                fee_pct=fee_pct,
                priority_sol=priority_sol,
            )
            if entry is not None:
                yield entry
            decision += step


def _priced_series(
    session: Session, *, asset_id: int, end: datetime
) -> list[tuple[datetime, float]]:
    """The asset's snapshots as (ts, price), point-in-time at ``end``.

    Only snapshots that can price an entry or a path point survive: a print
    with no positive price is not an observation of the market. Rows the
    loader returns are already gated on ``observed_at <= end``; the filter
    is restated here so the causality contract does not silently depend on
    the loader's internals.
    """
    rows = point_in_time_market_rows(session, asset_id=asset_id, decision_ts=end)
    series = [
        (ensure_utc(row.ts), float(row.price_usd))
        for row in rows
        if ensure_utc(row.observed_at) <= end and row.price_usd and row.price_usd > 0
    ]
    series.sort(key=lambda item: item[0])
    return series


def _entry_at(
    series: list[tuple[datetime, float]],
    stamps: list[datetime],
    *,
    asset_id: int,
    decision_ts: datetime,
    end: datetime,
    window: timedelta,
    size_sol: Decimal,
    slip_pct: Decimal,
    fee_pct: Decimal,
    priority_sol: Decimal,
) -> ReplayEntry | None:
    entry_idx = bisect_right(stamps, decision_ts)
    if entry_idx >= len(series):
        return None  # no snapshot strictly after the decision
    entry_ts, raw_price = series[entry_idx]
    if entry_ts + window > end:
        return None  # forward window not fully observed inside the replay
    forward_rows = tuple(series[entry_idx + 1 : bisect_right(stamps, decision_ts + window)])
    fill = simulate_buy(
        size_sol=size_sol,
        quote_price=Decimal(str(raw_price)),
        slip_pct=slip_pct,
        fee_pct=fee_pct,
        priority_sol=priority_sol,
    )
    entry_price = float(fill.effective_price)
    path = forward_path(entry_price, decision_ts, list(forward_rows))
    if path is None:
        return None  # no forward rows: nothing to attach, skip rather than fabricate
    return ReplayEntry(
        asset_id=asset_id,
        decision_ts=decision_ts,
        entry_ts=entry_ts,
        entry_price=entry_price,
        path=path,
        forward_rows=forward_rows,
    )
