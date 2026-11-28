"""Rule-based exit policy v1 (mission-refactor Task 9, spec §4).

The policy that Task 8's exit evaluator grades. A pure state machine
over a held position — no DB access inside ``on_snapshot`` — applying
the spec's layers in priority order on each snapshot:

1. **Override** — an LP-removal event exits everything now.
2. **Floor** — fixed gain-ladder rungs sell fixed fractions of the
   original position no matter what (the benchmark-grade behavior,
   guaranteed capture).
3. **Completion (runner)** — once the trail is active (the position
   armed, or every rung hit), a giveback of ``exit_trail_fraction``
   from the peak price exits the remainder in full. The trail is
   checked before the armed tranche: a tranche is pacing, the trail
   is completion — when the giveback has happened, selling another
   depth-capped slice instead of completing would bleed the exit
   down the decay the trail exists to cut off.
4. **Pacing** — while armed and not completed, tranches sell the
   position down: sized at most ``exit_max_depth_fraction`` of
   current quote depth when pacing is enabled, else the ladder's
   fixed fraction of the original position (the Task 15 fallback).
   Task 16's distribution accelerate votes ride this layer only:
   when a snapshot's ``DistributionVotes.accelerates`` is true
   (≥ 2 of 3 distribution signals active), the armed tranche is
   scaled by the pace multiplier — the votes pull the next tranche
   forward. Votes never arm the ladder and never fire an exit
   alone; on an unarmed position they change nothing.

Arming (spec §4 layer 2) requires flow rollover (net-flow ratio at or
below ``exit_nfr_arm_threshold``, Task 5's ``net_inflow_ratio``
semantics) AND a unique-buyer stall (delta <= 0) on the SAME snapshot;
arming never sells by itself and never disarms.

Pacing gate (Task 15, binding): the live depth-lead study is
UNMEASURED, so ``exit_pacing_enabled`` ships default False (see
``common.config``). The depth-cap math below is live code, exercised
by tests with pacing explicitly enabled; the shipped default takes
the fixed-fraction fallback.

Tranche valuation: the remaining position is marked to market in
entry-notional units — ``qty_remaining * snap.price / entry_price``
— the same scaled convention ``trading.costs`` uses for replay
proceeds ratios, so a tranche's quote value and the snapshot's quote
depth must be expressed in the same currency by the caller.

``replay_exit_rows`` is the row builder that feeds the evaluator: it
runs this policy, the fixed ladder, and the fixed-horizon benchmark
over Task 4 replay entries, netting every sell through
``simulate_sell`` at the replay's own cost constants, and grades
against Task 7's executable peak recomputed per entry via
``forecast.harvest.harvest_path`` (the ``executable_peak_gain_s10``
semantics applied to the entry's own impact-adjusted entry price and
forward rows). Stored harvest labels are NOT read: they are keyed to
label-engine decision points at raw snapshot prices, a different
entry basis than the replay's impact-adjusted entries. Any position
still open at the window's last print is liquidated at that print —
for the policy and both benchmarks alike — so gains are comparable.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from backtest.replay import ReplayEntry, iter_replay_entries
from backtest.runner import point_in_time_market_rows
from common.config import Settings
from common.time import ensure_utc
from forecast.harvest import WINDOW_REFERENCE_FRACTION, harvest_path
from forecast.labels import aligned_harvest_rows, point_in_time_depth_series
from storage import models
from trading.costs import q_sol, simulate_sell

if TYPE_CHECKING:
    from features.factory import DistributionVotes
    from validation.harness import ExitRow

# The replay's cost basis (Task 4's iter_replay_entries defaults):
# entries are priced and every strategy's sells are netted at these
# constants, so policy / ladder / horizon gains share one cost model.
_ENTRY_SIZE_SOL = Decimal("1")
_SLIP_PCT = Decimal("5")
_FEE_PCT = Decimal("1")
_PRIORITY_SOL = Decimal("0")


# ── The policy state machine ───────────────────────────────────────────────


@dataclass
class PositionState:
    entry_price: float
    qty_remaining: float  # fraction of original position, 1.0 -> 0.0
    peak_price: float
    rungs_hit: int = 0
    armed: bool = False
    exited: bool = False


# Task 16 pace multiplier: accelerating distribution votes scale an
# ARMED tranche by this factor (the tranche is still clamped to the
# full remainder). No Settings field exists for it in the plan; the
# pinned tests fix the value at 2x (0.25 -> 0.5 fixed-fraction,
# 0.10 -> 0.20 depth-capped).
_ACCELERATE_PACE_MULTIPLIER = 2.0


@dataclass(frozen=True)
class ExitSnapshot:
    ts: datetime
    price: float
    quote_depth_usd: float
    net_flow_ratio: float  # Task 5 net-inflow semantics, latest window
    unique_buyers_delta: float  # change since previous snapshot
    lp_removed: bool = False
    # Task 16 layer-3 accelerate votes; None = no distribution
    # evidence on this snapshot, which is neutral (never active).
    distribution_votes: DistributionVotes | None = None


@dataclass(frozen=True)
class ExitDecision:
    action: str  # "hold" | "sell_tranche" | "exit_all"
    fraction: float  # of qty_remaining to sell now
    reason: str  # "lp_removal" | "ladder_rung" | "armed_tranche" | "ev_trail" | "hold"


def _rungs_crossed(
    rungs: tuple[float, ...], entry_price: float, price: float, already_hit: int
) -> int:
    """Rungs whose gain threshold ``price`` has reached, beyond those hit."""
    total = sum(1 for rung in rungs if price >= entry_price * (1.0 + rung))
    return max(0, total - already_hit)


class ExitPolicy:
    """Layered exit rules over a :class:`PositionState`.

    ``on_snapshot`` APPLIES its decision to the state (peak, arming,
    rungs, qty_remaining, exited) and returns it; callers read the
    sold fraction as the ``qty_remaining`` delta around the call.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def on_snapshot(self, state: PositionState, snap: ExitSnapshot) -> ExitDecision:
        settings = self.settings
        if state.exited or state.qty_remaining <= 0.0:
            return ExitDecision("hold", 0.0, "hold")
        if snap.price > state.peak_price:
            state.peak_price = snap.price
        # Arming: flow rollover AND buyer stall on the same snapshot;
        # once armed, never disarmed.
        if (
            not state.armed
            and snap.net_flow_ratio <= settings.exit_nfr_arm_threshold
            and snap.unique_buyers_delta <= 0.0
        ):
            state.armed = True

        # 1. Hard override: LP removal exits everything now.
        if snap.lp_removed:
            return self._apply(state, 1.0, "exit_all", "lp_removal")

        # 2. Ladder floor: each newly crossed rung sells its fixed
        # fraction of the ORIGINAL position, once.
        crossed = _rungs_crossed(
            settings.exit_ladder_rungs, state.entry_price, snap.price, state.rungs_hit
        )
        if crossed:
            state.rungs_hit += crossed
            sell_original = min(settings.exit_ladder_fraction * crossed, state.qty_remaining)
            return self._apply(
                state,
                sell_original / state.qty_remaining,
                "sell_tranche",
                "ladder_rung",
            )

        # 3. EV-trail runner: active only when armed or all rungs hit;
        # a giveback of the trail fraction from the peak completes.
        trail_active = state.armed or state.rungs_hit >= len(settings.exit_ladder_rungs)
        if trail_active and snap.price <= state.peak_price * (1.0 - settings.exit_trail_fraction):
            return self._apply(state, 1.0, "exit_all", "ev_trail")

        # 4. Armed tranches: depth-paced when pacing is enabled, else
        # the ladder's fixed fraction of the original position.
        if state.armed:
            if settings.exit_pacing_enabled:
                value = state.qty_remaining * snap.price / state.entry_price
                fraction = (
                    min(
                        1.0,
                        settings.exit_max_depth_fraction * snap.quote_depth_usd / value,
                    )
                    if value > 0.0
                    else 1.0
                )
            else:
                fraction = min(1.0, settings.exit_ladder_fraction / state.qty_remaining)
            # Layer 3 acceleration: distribution votes scale the
            # armed tranche (pacing-sized or fixed-fraction alike).
            # This branch is reachable only when armed — unarmed
            # votes never reach sizing and change nothing.
            if snap.distribution_votes is not None and snap.distribution_votes.accelerates:
                fraction = min(1.0, fraction * _ACCELERATE_PACE_MULTIPLIER)
            if fraction > 0.0:
                return self._apply(state, fraction, "sell_tranche", "armed_tranche")

        return ExitDecision("hold", 0.0, "hold")

    @staticmethod
    def _apply(state: PositionState, fraction: float, action: str, reason: str) -> ExitDecision:
        fraction = min(1.0, max(0.0, fraction))
        state.qty_remaining -= fraction * state.qty_remaining
        if state.qty_remaining <= 1e-12:
            state.qty_remaining = 0.0
            state.exited = True
        return ExitDecision(action, fraction, reason)


# ── Replay row builder ─────────────────────────────────────────────────────


def _entry_qty(entry_price: float) -> Decimal:
    """Scaled position quantity from the entry's simulate_buy fill:
    net size (after fee + priority) over the effective entry price —
    the same scaled convention ``trading.costs`` documents."""
    fee = q_sol(_ENTRY_SIZE_SOL * _FEE_PCT / Decimal("100"))
    net = _ENTRY_SIZE_SOL - fee - _PRIORITY_SOL
    return net / Decimal(str(entry_price))


def _sell_proceeds(qty_total: Decimal, fraction_of_original: float, price: float) -> Decimal:
    proceeds, _fill = simulate_sell(
        qty=qty_total * Decimal(str(fraction_of_original)),
        quote_price=Decimal(str(price)),
        slip_pct=_SLIP_PCT,
        fee_pct=_FEE_PCT,
        priority_sol=_PRIORITY_SOL,
    )
    return proceeds


def _gain(proceeds: Decimal) -> float:
    return float(proceeds / _ENTRY_SIZE_SOL) - 1.0


def _value_at(series: list[tuple[datetime, float]], ts: datetime) -> float | None:
    """Last series value at or before ``ts`` (series sorted by ts)."""
    stamps = [stamp for stamp, _ in series]
    index = bisect_right(stamps, ts)
    if index == 0:
        return None
    return series[index - 1][1]


def _pit_liquidity_rows(
    session: Session, *, asset_id: int, observed_by: datetime
) -> list[models.LiquiditySnapshot]:
    """The asset's liquidity prints as known at ``observed_by`` — the
    same pairs->pools join and point-in-time gate as
    ``forecast.labels.point_in_time_depth_series``, full rows."""
    pairs = list(session.scalars(select(models.Pair).where(models.Pair.base_asset_id == asset_id)))
    pool_ids = sorted({pair.pool_id for pair in pairs if pair.pool_id is not None})
    if not pool_ids:
        return []
    return list(
        session.scalars(
            select(models.LiquiditySnapshot)
            .where(
                models.LiquiditySnapshot.pool_id.in_(pool_ids),
                models.LiquiditySnapshot.ts <= observed_by,
                models.LiquiditySnapshot.observed_at <= observed_by,
            )
            .order_by(models.LiquiditySnapshot.ts, models.LiquiditySnapshot.id)
        )
    )


def _net_flow_ratio(
    liquidity_rows: list[models.LiquiditySnapshot], ts: datetime, lookback: timedelta
) -> float | None:
    """Task 5's ``net_inflow_ratio`` at an arbitrary snapshot time:
    ``(reserve_now - reserve_prev) / reserve_prev`` with reserve_now
    the latest print at or before ``ts`` and reserve_prev the latest
    at or before ``ts - lookback``; ``reserve_quote`` primary,
    ``reserve_usd`` fallback (``FeatureFactory._net_inflow``
    semantics, evaluated per snapshot rather than per decision)."""
    for attr in ("reserve_quote", "reserve_usd"):
        series = [
            (ensure_utc(row.ts), float(value))
            for row in liquidity_rows
            if (value := getattr(row, attr, None)) is not None
        ]
        if not series:
            continue
        now_value = _value_at(series, ts)
        prior_value = _value_at(series, ts - lookback)
        if now_value is None or prior_value is None or prior_value <= 0:
            continue
        return (now_value - prior_value) / prior_value
    return None


def _buyers_series(
    session: Session, *, asset_id: int, observed_by: datetime
) -> list[tuple[datetime, float]]:
    """Per-print unique-buyer estimates: the market rows' ``buys`` —
    the series ``unique_buyers_estimate`` is the latest print of."""
    rows = point_in_time_market_rows(session, asset_id=asset_id, decision_ts=observed_by)
    series = [(ensure_utc(row.ts), float(row.buys)) for row in rows if row.buys is not None]
    series.sort(key=lambda item: item[0])
    return series


def _removal_times(
    session: Session, *, asset_id: int, start: datetime, end: datetime
) -> list[datetime]:
    rows = session.scalars(
        select(models.LiquidityRemovalEvent)
        .where(
            models.LiquidityRemovalEvent.asset_id == asset_id,
            models.LiquidityRemovalEvent.ts > start,
            models.LiquidityRemovalEvent.ts <= end,
            models.LiquidityRemovalEvent.observed_at <= end,
        )
        .order_by(models.LiquidityRemovalEvent.ts)
    ).all()
    return [ensure_utc(row.ts) for row in rows]


def _build_snapshots(
    entry: ReplayEntry,
    aligned: list[tuple[datetime, float, float | None]],
    *,
    liquidity_rows: list[models.LiquiditySnapshot],
    buyers: list[tuple[datetime, float]],
    removal_times: list[datetime],
    lookback: timedelta,
) -> list[ExitSnapshot]:
    """One ExitSnapshot per forward row of the entry.

    Missing-data substitutions are conservative-by-construction and
    recorded here rather than hidden: no measurable net flow reads
    0.0 (at the arm threshold — it can only arm together with a
    measured buyer stall), a missing buyer print reads a 0.0 delta
    against the last measured estimate, and no depth print reads 0.0
    depth (pacing then sells the whole remainder — with pacing off,
    the shipped default, depth never sizes a tranche at all).
    """
    snapshots: list[ExitSnapshot] = []
    prev_buyers = _value_at(buyers, entry.entry_ts)
    boundary = entry.decision_ts
    for (ts, price), (_ts, _price, depth) in zip(entry.forward_rows, aligned, strict=True):
        estimate = _value_at(buyers, ts)
        if estimate is not None and prev_buyers is not None:
            buyers_delta = estimate - prev_buyers
        else:
            buyers_delta = 0.0
        if estimate is not None:
            prev_buyers = estimate
        net_flow = _net_flow_ratio(liquidity_rows, ts, lookback)
        snapshots.append(
            ExitSnapshot(
                ts=ts,
                price=price,
                quote_depth_usd=float(depth) if depth else 0.0,
                net_flow_ratio=net_flow if net_flow is not None else 0.0,
                unique_buyers_delta=float(buyers_delta),
                lp_removed=any(boundary < removed <= ts for removed in removal_times),
            )
        )
        boundary = ts
    return snapshots


def _policy_gain(policy: ExitPolicy, entry: ReplayEntry, snapshots: list[ExitSnapshot]) -> float:
    qty_total = _entry_qty(entry.entry_price)
    state = PositionState(
        entry_price=entry.entry_price, qty_remaining=1.0, peak_price=entry.entry_price
    )
    proceeds = Decimal(0)
    for snap in snapshots:
        before = state.qty_remaining
        policy.on_snapshot(state, snap)
        sold = before - state.qty_remaining
        if sold > 0.0:
            proceeds += _sell_proceeds(qty_total, sold, snap.price)
    if state.qty_remaining > 0.0 and snapshots:
        proceeds += _sell_proceeds(qty_total, state.qty_remaining, snapshots[-1].price)
    return _gain(proceeds)


def _ladder_gain(entry: ReplayEntry, *, settings: Settings) -> float:
    """The fixed-ladder benchmark: rungs only, remainder out at the
    window's last print. No arming, no tranches, no trail."""
    qty_total = _entry_qty(entry.entry_price)
    remaining = 1.0
    rungs_hit = 0
    proceeds = Decimal(0)
    for _ts, price in entry.forward_rows:
        crossed = _rungs_crossed(settings.exit_ladder_rungs, entry.entry_price, price, rungs_hit)
        if crossed:
            rungs_hit += crossed
            sell = min(settings.exit_ladder_fraction * crossed, remaining)
            proceeds += _sell_proceeds(qty_total, sell, price)
            remaining -= sell
        if remaining <= 0.0:
            break
    if remaining > 0.0 and entry.forward_rows:
        proceeds += _sell_proceeds(qty_total, remaining, entry.forward_rows[-1][1])
    return _gain(proceeds)


def replay_exit_rows(
    session: Session, *, start: datetime, end: datetime, settings: Settings
) -> list[ExitRow]:
    """Replay the exit policy and its benchmarks over Task 4 entries.

    One ``ExitRow`` per replay entry that earns an executable episode
    (a depth path exists — no depth path, no row: absence, not a
    fabricated zero, exactly as Task 7's labels treat it). Entries
    whose executable peak never exceeds their entry price are also
    skipped: there was nothing to capture, and captured fraction is
    degenerate by construction there (Task 8's ``captured_fraction``
    special-cases ``pev <= 0`` for exactly this reason) — grading a
    capture skill on windows with no executable upside would read
    the no-upside mass, not the policy.
    """
    from validation.harness import ExitRow

    start = ensure_utc(start)
    end = ensure_utc(end)
    asset_ids = sorted(
        session.scalars(
            select(models.Asset.id)
            .join(models.Pair, models.Pair.base_asset_id == models.Asset.id)
            .distinct()
        ).all()
    )
    if not asset_ids:
        return []
    entries = iter_replay_entries(
        session,
        asset_ids=asset_ids,
        start=start,
        end=end,
        forward_hours=settings.forecast_forward_hours,
        size_sol=_ENTRY_SIZE_SOL,
        slip_pct=_SLIP_PCT,
        fee_pct=_FEE_PCT,
        priority_sol=_PRIORITY_SOL,
    )
    lookback = timedelta(hours=settings.feature_net_inflow_lookback_hours)
    policy = ExitPolicy(settings)
    depth_cache: dict[int, list[tuple[datetime, float | None]]] = {}
    liquidity_cache: dict[int, list[models.LiquiditySnapshot]] = {}
    buyers_cache: dict[int, list[tuple[datetime, float]]] = {}
    removal_cache: dict[int, list[datetime]] = {}
    rows: list[ExitRow] = []
    for entry in entries:
        asset_id = entry.asset_id
        if asset_id not in depth_cache:
            depth_cache[asset_id] = point_in_time_depth_series(
                session, asset_id=asset_id, observed_by=end
            )
            liquidity_cache[asset_id] = _pit_liquidity_rows(
                session, asset_id=asset_id, observed_by=end
            )
            buyers_cache[asset_id] = _buyers_series(session, asset_id=asset_id, observed_by=end)
            removal_cache[asset_id] = _removal_times(
                session, asset_id=asset_id, start=start, end=end
            )
        aligned = aligned_harvest_rows(list(entry.forward_rows), depth_cache[asset_id])
        hpath = harvest_path(entry.entry_price, entry.decision_ts, aligned, settings=settings)
        if hpath is None:
            continue
        if hpath.executable_peak_gain_pct[WINDOW_REFERENCE_FRACTION] <= 0.0:
            continue  # no executable upside in this window: not an episode
        snapshots = _build_snapshots(
            entry,
            aligned,
            liquidity_rows=liquidity_cache[asset_id],
            buyers=buyers_cache[asset_id],
            removal_times=removal_cache[asset_id],
            lookback=lookback,
        )
        qty_total = _entry_qty(entry.entry_price)
        horizon_proceeds = _sell_proceeds(qty_total, 1.0, entry.forward_rows[-1][1])
        rows.append(
            ExitRow(
                asset_id=asset_id,
                decision_ts=entry.decision_ts,
                pev_gain_pct=hpath.executable_peak_gain_pct[WINDOW_REFERENCE_FRACTION],
                clairvoyant_gain_pct=entry.path.max_gain_pct,
                policy_gain_pct=_policy_gain(policy, entry, snapshots),
                ladder_gain_pct=_ladder_gain(entry, settings=settings),
                horizon_gain_pct=_gain(horizon_proceeds),
            )
        )
    return rows
