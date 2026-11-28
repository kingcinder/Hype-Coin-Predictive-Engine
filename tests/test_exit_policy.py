"""Mission-refactor Task 9: rule-based exit policy v1 + replay rows.

The policy (spec §4) is a layered state machine over a held position:
(1) LP-removal hard override, (2) fixed gain-ladder floor, (3) once
armed by flow rollover AND buyer stall, depth-paced tranches (or the
ladder's fixed fractions while pacing is gated off), (4) an EV-trail
runner that completes the exit on a peak giveback. ``on_snapshot`` is
pure (no DB); all assembly lives in ``replay_exit_rows``, which replays
Task 4 entries, prices every strategy's sells through
``trading.costs.simulate_sell``, and grades against Task 7's
executable peak (``forecast.harvest`` semantics at the 10% reference).

Pacing gate (Task 15, binding): the live depth-lead study is
UNMEASURED, so ``exit_pacing_enabled`` ships default False — the
pacing math is pinned here with pacing explicitly enabled, and the
shipped default is pinned as its own test.

Acceptance (plan Step 5): the policy is scored against the fixed
ladder on held-out synthetic episodes (generator seeds below are
disjoint from every fixture above them) and must strict-beat the
ladder on BOTH the median and the p25 captured-fraction cells.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from common.config import Settings
from forecast.harvest import harvest_path
from forecast.labels import aligned_harvest_rows
from storage import models
from storage.repository import (
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference
from trading.costs import simulate_buy, simulate_sell
from trading.exit_policy import (
    ExitPolicy,
    ExitSnapshot,
    PositionState,
    replay_exit_rows,
)
from validation.harness import evaluate_exit, load_exit_rows

T0 = datetime(2026, 5, 1, 0, 0)  # naive, the SQLite seeding idiom


# ── fixtures ───────────────────────────────────────────────────────────────


def snap(
    *,
    hour: float = 0.0,
    price: float = 1.0,
    depth: float = 100_000.0,
    nfr: float = 0.5,
    buyers_delta: float = 5.0,
    lp_removed: bool = False,
) -> ExitSnapshot:
    return ExitSnapshot(
        ts=T0 + timedelta(hours=hour),
        price=price,
        quote_depth_usd=depth,
        net_flow_ratio=nfr,
        unique_buyers_delta=buyers_delta,
        lp_removed=lp_removed,
    )


def state(**overrides: float) -> PositionState:
    fields: dict = {
        "entry_price": 1.0,
        "qty_remaining": 1.0,
        "peak_price": 1.0,
        "rungs_hit": 0,
        "armed": False,
        "exited": False,
    }
    fields.update(overrides)
    return PositionState(**fields)


@pytest.fixture()
def policy() -> ExitPolicy:
    """The policy as shipped: default Settings (pacing gated OFF)."""
    return ExitPolicy(Settings())


@pytest.fixture()
def pacing_policy() -> ExitPolicy:
    return ExitPolicy(Settings(exit_pacing_enabled=True))


# ── Settings: the Task 15 gate on the pacing default ───────────────────────


def test_exit_settings_defaults_pin_the_plan_and_task15_gate():
    settings = Settings()
    assert settings.exit_ladder_rungs == (0.5, 1.0, 2.0, 4.0)
    assert settings.exit_ladder_fraction == 0.25
    assert settings.exit_max_depth_fraction == 0.125
    assert settings.exit_trail_fraction == 0.15
    assert settings.exit_nfr_arm_threshold == 0.0
    # Task 15's live depth-lead study is UNMEASURED (no recorded
    # median lead > 0), so pacing ships OFF; tranches fall back to
    # the ladder's fixed fractions until the study clears it.
    assert settings.exit_pacing_enabled is False


# ── Layer 7: LP-removal override ───────────────────────────────────────────


def test_lp_removal_exits_all_immediately(policy):
    st = state()
    decision = policy.on_snapshot(st, snap(price=1.1, lp_removed=True))
    assert (decision.action, decision.reason) == ("exit_all", "lp_removal")
    assert decision.fraction == pytest.approx(1.0)
    assert st.exited and st.qty_remaining == 0.0


def test_lp_removal_outranks_a_crossed_rung(policy):
    st = state()
    decision = policy.on_snapshot(st, snap(price=1.6, lp_removed=True))
    assert decision.reason == "lp_removal"
    assert st.rungs_hit == 0  # the rung was bypassed, not consumed


def test_exited_position_holds_forever(policy):
    st = state()
    policy.on_snapshot(st, snap(lp_removed=True))
    decision = policy.on_snapshot(st, snap(price=9.0, hour=1))
    assert (decision.action, decision.fraction) == ("hold", 0.0)


# ── Layer 1: the ladder floor ──────────────────────────────────────────────


def test_ladder_rung_sells_quarter_of_original(policy):
    st = state()
    decision = policy.on_snapshot(st, snap(price=1.6))  # +60% crosses rung 0.5
    assert decision.reason == "ladder_rung"
    assert decision.action == "sell_tranche"
    assert decision.fraction == pytest.approx(0.25)
    assert st.rungs_hit == 1
    assert st.qty_remaining == pytest.approx(0.75)


def test_ladder_rung_fires_once(policy):
    st = state()
    policy.on_snapshot(st, snap(price=1.6))
    decision = policy.on_snapshot(st, snap(price=1.7, hour=1))
    assert decision.action == "hold"
    assert st.rungs_hit == 1


def test_gap_up_crosses_multiple_rungs_in_one_snapshot(policy):
    st = state()
    decision = policy.on_snapshot(st, snap(price=3.2))  # rungs 0.5, 1.0, 2.0
    assert decision.reason == "ladder_rung"
    assert decision.fraction == pytest.approx(0.75)
    assert st.rungs_hit == 3
    assert st.qty_remaining == pytest.approx(0.25)


# ── Layer 2: arming (flow rollover AND buyer stall, same snapshot) ─────────


def test_no_arming_on_flow_rollover_alone(policy):
    # NFR negative but buyers still growing: not armed, no tranche.
    st = state()
    decision = policy.on_snapshot(st, snap(nfr=-0.2, buyers_delta=5.0))
    assert decision.action == "hold"
    assert st.armed is False


def test_no_arming_on_buyer_stall_alone(policy):
    st = state()
    decision = policy.on_snapshot(st, snap(nfr=0.3, buyers_delta=-3.0))
    assert decision.action == "hold"
    assert st.armed is False


def test_arming_needs_both_and_never_disarms(policy):
    st = state()
    policy.on_snapshot(st, snap(price=1.2, nfr=-0.2, buyers_delta=-3.0))
    assert st.armed is True
    # Healthy flow later does not disarm.
    policy.on_snapshot(st, snap(price=1.25, hour=1, nfr=0.9, buyers_delta=10.0))
    assert st.armed is True


# ── Layer 4: pacing — depth-capped tranches ────────────────────────────────


def test_armed_tranche_respects_depth_cap(pacing_policy):
    # The plan's worked example: position quote value 10_000, depth
    # 8_000 -> one tranche sells at most 0.125 * 8_000 = 1_000 quote
    # value = 10% of the position. Position value is marked to market
    # in entry-notional units: qty_remaining * price / entry_price
    # = 1.0 * 10_000 / 1.0 = 10_000.
    st = state(peak_price=10_000.0, rungs_hit=4, armed=True)
    decision = pacing_policy.on_snapshot(st, snap(price=10_000.0, depth=8_000.0))
    assert (decision.action, decision.reason) == ("sell_tranche", "armed_tranche")
    assert decision.fraction <= 0.10 + 1e-9
    assert decision.fraction == pytest.approx(0.10)
    assert st.qty_remaining == pytest.approx(0.9)


def test_pacing_off_tranche_falls_back_to_fixed_fraction(policy):
    # Shipped default (pacing gated off by Task 15): an armed tranche
    # sells the ladder's fixed fraction of the ORIGINAL position —
    # 0.25 of original against 0.5 remaining is a 0.5 fraction of
    # remaining — regardless of how deep the pool is.
    st = state(peak_price=1.8, rungs_hit=1, qty_remaining=0.5, armed=True)
    decision = policy.on_snapshot(st, snap(price=1.8, depth=1e12))
    assert decision.reason == "armed_tranche"
    assert decision.fraction == pytest.approx(0.5)


def test_pacing_on_deep_pool_tranche_can_take_all_remaining(pacing_policy):
    st = state(peak_price=1.8, rungs_hit=1, qty_remaining=0.5, armed=True)
    decision = pacing_policy.on_snapshot(st, snap(price=1.8, depth=1e12))
    assert decision.reason == "armed_tranche"
    assert decision.fraction == pytest.approx(1.0)


# ── Layer 6: the EV-trail runner ───────────────────────────────────────────


def test_ev_trail_exits_after_peak_giveback(policy):
    # Armed; peak 4.00, price 3.30 (giveback 17.5% > trail 15%).
    st = state(peak_price=4.0, rungs_hit=3, qty_remaining=0.25, armed=True)
    decision = policy.on_snapshot(st, snap(price=3.30))
    assert (decision.action, decision.reason) == ("exit_all", "ev_trail")
    assert st.exited


def test_ev_trail_inactive_until_armed_or_all_rungs_hit(policy):
    # Same giveback, but the position never armed and a rung remains
    # (rungs_hit=3 of 4 at peak 4.0 — the 4.0-gain rung at 5.0 was
    # never crossed): the trail is not live — staging is
    # load-bearing (spec §4).
    st = state(peak_price=4.0, rungs_hit=3, qty_remaining=0.25, armed=False)
    decision = policy.on_snapshot(st, snap(price=3.30))
    assert decision.action == "hold"


def test_ev_trail_active_when_all_rungs_hit_even_unarmed(policy):
    st = state(peak_price=4.0, rungs_hit=4, qty_remaining=0.2, armed=False)
    decision = policy.on_snapshot(st, snap(price=3.30))
    assert (decision.action, decision.reason) == ("exit_all", "ev_trail")


def test_no_trail_exit_inside_the_trail_fraction(policy):
    st = state(peak_price=4.0, rungs_hit=3, qty_remaining=0.25, armed=True)
    # 3.50 is a 12.5% giveback < 15%: the runner keeps running (the
    # armed tranche may still sell, but it must not exit_all).
    decision = policy.on_snapshot(st, snap(price=3.50))
    assert decision.action != "exit_all"


# ── replay_exit_rows: the row builder over a seeded DB ─────────────────────
#
# Seeding idiom mirrors tests/test_depth_lead_study.py: hourly prints,
# observed_at == ts, one pair/pool per asset.


def _seed_series(
    session,
    *,
    symbol: str,
    prices: list[float],
    depths: list[float],
    buys: list[int],
):
    """One asset with hourly market + liquidity prints from T0."""
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"Addr{symbol}11111111111111111111111111111111111",
        symbol=symbol,
        name=symbol,
        first_seen_at=T0,
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"Quote{symbol}1111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=T0 - timedelta(days=365),
    )
    pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=f"Pair{symbol}1111111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0,
    )
    for hour, (price, depth, buy_count) in enumerate(zip(prices, depths, buys, strict=True)):
        ts = T0 + timedelta(hours=hour)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=1_000.0,
            buys=buy_count,
            sells=50,
        )
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            reserve_usd=depth,
            reserve_quote=depth,
        )
    session.commit()
    return asset, pool, source


def _pump_series() -> tuple[list[float], list[float], list[int]]:
    """49 hourly prints: rise to 3.4x at h6, decay to 1.2x by h24, flat."""
    prices = [1.0, 1.2, 1.5, 2.0, 2.6, 3.2, 3.4, 3.0, 2.6, 2.2, 1.9, 1.7, 1.55]
    prices += [1.55 - 0.035 * i for i in range(1, 12)]  # h13..h23 -> ~1.17
    prices += [1.17] * (49 - len(prices))
    depths = [5_000.0, 5_500.0, 6_000.0, 6_800.0, 7_600.0, 8_400.0, 9_000.0]
    depths += [9_000.0 - 350.0 * i for i in range(1, 18)]  # h7..h23 -> ~3_050
    depths += [3_050.0] * (49 - len(depths))
    buys = [100, 120, 145, 175, 210, 250, 290]
    buys += [290 - 18 * i for i in range(1, 18)]  # stalls, then declines
    buys += [30] * (49 - len(buys))
    return prices, depths, buys


def test_replay_exit_rows_grades_against_the_executable_peak(session):
    prices, depths, buys = _pump_series()
    asset, _pool, _source = _seed_series(
        session, symbol="PUMP", prices=prices, depths=depths, buys=buys
    )
    settings = Settings()
    rows = replay_exit_rows(session, start=T0, end=T0 + timedelta(hours=48), settings=settings)
    assert rows, "the seeded episode must earn replay rows"
    row = next(r for r in rows if r.decision_ts == T0.replace(tzinfo=UTC))
    assert row.asset_id == asset.id

    # Entry is the h1 print priced with impact: 1.2 * 1.05.
    entry_price = 1.2 * 1.05
    # PEV is Task 7's executable peak at the 10% reference, recomputed
    # here through the public harvest API over the same aligned rows
    # the label engines use (forward rows are h2..h24 for a T0 entry).
    forward_points = [(T0 + timedelta(hours=h), prices[h]) for h in range(2, 25)]
    depth_series = [(T0 + timedelta(hours=h), depths[h]) for h in range(49)]
    hpath = harvest_path(
        entry_price,
        T0.replace(tzinfo=UTC),
        aligned_harvest_rows(forward_points, depth_series),
        settings=settings,
    )
    assert hpath is not None
    assert row.pev_gain_pct == pytest.approx(hpath.executable_peak_gain_pct[0.10])
    # The executable peak never exceeds the raw price peak.
    assert row.clairvoyant_gain_pct == pytest.approx(3.4 / entry_price - 1.0)
    assert row.pev_gain_pct <= row.clairvoyant_gain_pct

    # The policy arms into the decay and exits the residual the ladder
    # rides to the window end: it must keep more of the episode.
    assert row.policy_gain_pct > row.ladder_gain_pct
    # Horizon benchmark: everything sold at the h24 print (1.17),
    # net of the same costs the entry paid.
    fill = simulate_buy(
        size_sol=Decimal("1"),
        quote_price=Decimal("1.2"),
        slip_pct=Decimal("5"),
        fee_pct=Decimal("1"),
        priority_sol=Decimal("0"),
    )
    proceeds, _ = simulate_sell(
        qty=fill.qty,
        quote_price=Decimal(str(prices[24])),
        slip_pct=Decimal("5"),
        fee_pct=Decimal("1"),
        priority_sol=Decimal("0"),
    )
    assert row.horizon_gain_pct == pytest.approx(float(proceeds) - 1.0)


def test_replay_exit_rows_lp_removal_exits_before_the_crash(session):
    prices = [1.0, 1.1, 1.3, 1.5, 1.7, 1.85, 2.0, 0.8, 0.3]
    prices += [0.3] * (49 - len(prices))
    # Healthy flow into the event (depth and buyers growing, so the
    # ladder is NOT armed early): the override — not a tranche — must
    # be what saves the position.
    depths = [5_000.0 + 300.0 * h for h in range(7)] + [3_000.0] * 42
    buys = [100 + 10 * h for h in range(7)] + [160] * 42
    asset, pool, source = _seed_series(
        session, symbol="LPX", prices=prices, depths=depths, buys=buys
    )
    event_ts = T0 + timedelta(hours=6)
    session.add(
        models.LiquidityRemovalEvent(
            asset_id=asset.id,
            pool_id=pool.id,
            source_id=source.id,
            chain_slug="solana",
            tx_hash="lpremove1",
            log_index=0,
            event_kind="remove_liquidity",
            block_number=1,
            ts=event_ts,
            observed_at=event_ts,
        )
    )
    session.commit()
    rows = replay_exit_rows(session, start=T0, end=T0 + timedelta(hours=48), settings=Settings())
    row = next(r for r in rows if r.decision_ts == T0.replace(tzinfo=UTC))
    # The override fired at the h6 print (2.0): most of the position
    # kept peak-adjacent proceeds instead of riding to 0.3.
    assert row.policy_gain_pct > 0.4
    assert row.policy_gain_pct > row.ladder_gain_pct + 0.5


def test_load_exit_rows_delegates_and_empty_db_is_empty(session):
    assert load_exit_rows(session) == []
    prices, depths, buys = _pump_series()
    _seed_series(session, symbol="PUMP", prices=prices, depths=depths, buys=buys)
    loaded = load_exit_rows(session)
    direct = replay_exit_rows(session, start=T0, end=T0 + timedelta(hours=48), settings=Settings())
    assert len(loaded) == len(direct) > 0
    assert {r.asset_id for r in loaded} == {r.asset_id for r in direct}


# ── Step 5 acceptance: median AND p25 strict beat on held-out episodes ─────
#
# The generator below is the held-out set: its seeds (20261128 + asset
# index) are disjoint from every fixture above (which are hand-built,
# seedless). Episodes are seeded as DB prints and graded end-to-end
# through replay_exit_rows -> evaluate_exit, exactly as the harness
# will grade live episodes.


def _episode_series(
    rng: random.Random, archetype: str
) -> tuple[list[float], list[float], list[int]]:
    hours = 49

    def path(control: list[tuple[int, float]]) -> list[float]:
        out = [0.0] * hours
        for (h0, v0), (h1, v1) in zip(control, control[1:], strict=False):
            for h in range(h0, h1 + 1):
                frac = 0.0 if h1 == h0 else (h - h0) / (h1 - h0)
                out[h] = v0 + (v1 - v0) * frac
        for h in range(control[-1][0] + 1, hours):
            out[h] = control[-1][1]
        return out

    def noisy(values: list[float], spread: float = 0.02) -> list[float]:
        return [max(0.05, v * rng.uniform(1 - spread, 1 + spread)) for v in values]

    if archetype == "pump_decay":
        peak_h = rng.randint(5, 10)
        peak = rng.uniform(2.3, 4.2)
        end = rng.uniform(0.9, 1.6)
        price = path([(0, 1.0), (peak_h, peak), (24, end), (48, end * 0.97)])
        base = rng.uniform(4.0, 9.0)
        depth = path(
            [(0, base), (peak_h, base * rng.uniform(1.5, 2.2)), (24, base * 0.6), (48, base * 0.55)]
        )
        buys = path(
            [(0, rng.uniform(80, 150)), (peak_h, rng.uniform(280, 420)), (24, 30.0), (48, 25.0)]
        )
    elif archetype == "grind":
        peak = rng.uniform(2.0, 3.0)
        price = path([(0, 1.0), (22, peak), (24, peak * 0.98), (48, peak * 0.96)])
        base = rng.uniform(4.0, 9.0)
        depth = path([(0, base), (24, base * 1.35), (48, base * 1.3)])
        buys = path([(0, 100.0), (20, 260.0), (24, 250.0), (48, 240.0)])
    elif archetype == "spike_crater":
        peak_h = rng.randint(2, 4)
        peak = rng.uniform(3.0, 5.0)
        floor = rng.uniform(0.5, 0.9)
        price = path([(0, 1.0), (peak_h, peak), (peak_h + 4, floor), (48, floor)])
        base = rng.uniform(4.0, 9.0)
        depth = path([(0, base), (peak_h, base * 2.0), (peak_h + 4, base * 0.3), (48, base * 0.3)])
        buys = path([(0, 120.0), (peak_h, 450.0), (peak_h + 3, 25.0), (48, 20.0)])
    elif archetype == "second_leg":
        peak1 = rng.uniform(2.2, 2.8)
        peak2 = rng.uniform(3.6, 4.6)
        dip = peak1 * rng.uniform(0.72, 0.8)
        end = rng.uniform(1.3, 1.8)
        price = path([(0, 1.0), (6, peak1), (9, dip), (15, peak2), (24, end), (48, end * 0.95)])
        base = rng.uniform(4.0, 9.0)
        depth = path(
            [
                (0, base),
                (6, base * 1.8),
                (9, base * 0.9),
                (15, base * 1.9),
                (24, base * 0.7),
                (48, base * 0.65),
            ]
        )
        buys = path([(0, 100.0), (6, 320.0), (9, 140.0), (15, 380.0), (24, 40.0), (48, 30.0)])
    else:  # dud
        price = [rng.uniform(0.92, 1.15) for _ in range(hours)]
        price[0] = 1.0
        base = rng.uniform(4.0, 9.0)
        depth = [base * rng.uniform(0.95, 1.05) for _ in range(hours)]
        buys = [int(rng.uniform(90, 110)) for _ in range(hours)]
        return price, depth, buys
    return noisy(price), noisy(depth, 0.03), [max(1, int(round(b))) for b in buys]


def test_acceptance_policy_beats_ladder_on_median_and_p25(session):
    archetypes = (
        ["pump_decay"] * 20
        + ["grind"] * 10
        + ["spike_crater"] * 8
        + ["second_leg"] * 4
        + ["dud"] * 6
    )
    for i, archetype in enumerate(archetypes):
        rng = random.Random(20261128 + i)
        prices, depths, buys = _episode_series(rng, archetype)
        _seed_series(session, symbol=f"AC{i:02d}", prices=prices, depths=depths, buys=buys)
    rows = replay_exit_rows(session, start=T0, end=T0 + timedelta(hours=48), settings=Settings())
    assert len(rows) >= 30
    cells = evaluate_exit(rows, regime="synthetic_acceptance", min_samples=30)
    by_metric = {c.metric: c for c in cells if c.output == "exit_policy"}
    for metric in ("median_captured_fraction", "p25_captured_fraction"):
        cell = by_metric[metric]
        assert cell.verdict in {"better_than_baseline", "beats_baseline"}, (
            f"{metric}: policy {cell.value:.4f} vs ladder {cell.baseline_value:.4f} "
            f"-> {cell.verdict} (n={cell.n})"
        )
