"""Task 17: exit-window liquidity labels + cohort-conditional stage.

(a) The exit-window label — "exit feasible for size X at t" — as a
standalone, queryable label beside the Task 7 harvest labels. For each
reference size f, feasibility is judged at that size's executable-peak
row t*_EV(f) (the moment a policy most wants to sell that size), and
all three legs must hold there: depth still >= 50% of the episode's
peak depth, trailing volume still >= the floor share of the episode's
peak trailing volume, and the constant-product fill of f * D_peak
against D(t*) implying slippage at or under the cap. A peak you cannot
sell into is not a peak, at any size.

(b) The cohort-conditional second stage: the phase-conditioned peak
hazards refit on the IGNITED cohort only — decision points at/after
the asset's IGNITION transition. Pre-ignition rows teach the hazard
what unignited coins do; the second stage exists to remove exactly
that contamination, so a never-ignited asset contributes no samples
and the seeding phase earns no second-stage score.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from common.config import get_settings
from forecast.cohort_exit import fit_cohort_peak_hazards, harvest_window_closing_hazard
from forecast.harvest import exit_window_feasible
from forecast.hazard import HazardFit
from forecast.labels import (
    LABEL_COLLAPSE,
    LABEL_EXECUTABLE_PEAK_GAIN_S10,
    LABEL_EXIT_WINDOW_FEASIBLE_S02,
    LABEL_EXIT_WINDOW_FEASIBLE_S10,
    LABEL_EXIT_WINDOW_FEASIBLE_S25,
    LABEL_IGNITION,
    LabelEngine,
    exit_window_label_specs,
    seed_labels_at_feature_timestamps,
    upsert_label,
)
from storage import models
from storage.repository import (
    get_or_create_source,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    upsert_asset,
    upsert_feature,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0)  # naive for SQLite compatibility
START = datetime(2026, 11, 20, tzinfo=UTC)


def _rows(points: list[tuple[float, float, float | None, float | None]]):
    return [
        (START + timedelta(hours=h), price, depth, volume) for h, price, depth, volume in points
    ]


# ── the three legs, at each size's executable peak ───────────────────────────


def test_exit_window_label_requires_all_three_legs():
    # Depth constant at the 10_000 peak, volume at peak share throughout:
    # the only failing leg anywhere is cost at the largest size.
    # D_peak = 10_000; notionals N(f) = 200 / 1_000 / 2_500. EV peaks at
    # +1h (price 2.00) for every size. Implied slippage N / (D + N):
    #   s02: 200/10_200 = 1.96% <= 10%  -> feasible
    #   s10: 1_000/11_000 = 9.09% <= 10% -> feasible
    #   s25: 2_500/12_500 = 20.0% > 10%  -> NOT feasible
    rows = _rows([(0, 1.00, 10_000, 1_000), (1, 2.00, 10_000, 1_000), (2, 1.50, 10_000, 1_000)])
    feasible = exit_window_feasible(rows, settings=get_settings())
    assert feasible == {0.02: True, 0.10: True, 0.25: False}
    specs = dict(exit_window_label_specs(feasible))
    assert specs == {
        LABEL_EXIT_WINDOW_FEASIBLE_S02: "1",
        LABEL_EXIT_WINDOW_FEASIBLE_S10: "1",
        LABEL_EXIT_WINDOW_FEASIBLE_S25: "0",
    }


def test_exit_window_depth_leg_fails_at_thin_peak():
    # The price peak (+1h, 3.00) prints on a drained pool (4_000 <
    # 50% of the 10_000 peak depth). At the s02 executable peak — also
    # +1h: 3.00 * 4_000 / 4_200 = 2.857 beats every deep row — the
    # depth leg fails, so even the smallest size is not exitable there.
    rows = _rows([(0, 1.00, 10_000, 1_000), (1, 3.00, 4_000, 1_000), (2, 1.20, 10_000, 1_000)])
    feasible = exit_window_feasible(rows, settings=get_settings())
    assert feasible is not None
    assert feasible[0.02] is False


def test_exit_window_volume_leg_fails_when_pool_abandoned():
    # Depth holds at the peak row, but trailing volume there has
    # decayed to 100 against an episode peak of 1_000 (floor share
    # 0.25 -> floor 250): the pool is no longer trading at the moment
    # of the peak, so the exit window is not feasible.
    rows = _rows([(0, 1.00, 10_000, 1_000), (1, 2.00, 10_000, 100), (2, 1.50, 10_000, 900)])
    feasible = exit_window_feasible(rows, settings=get_settings())
    assert feasible is not None
    assert feasible[0.02] is False
    assert feasible[0.10] is False


def test_exit_window_absent_without_depth_or_volume_path():
    # No usable depth anywhere -> absence, not zeros (Task 7's rule).
    rows = _rows([(0, 1.00, None, 1_000), (1, 2.00, 0, 1_000)])
    assert exit_window_feasible(rows, settings=get_settings()) is None
    # Depth path but no volume print anywhere -> absence.
    rows = _rows([(0, 1.00, 10_000, None), (1, 2.00, 10_000, None)])
    assert exit_window_feasible(rows, settings=get_settings()) is None
    # Empty episode -> absence.
    assert exit_window_feasible([], settings=get_settings()) is None


# ── engine wiring ────────────────────────────────────────────────────────────


def _seed_asset(
    session,
    *,
    symbol: str,
    points: list[tuple[int, float, float]],
):
    """points: (hour, price, volume) — market snapshots with volume."""
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
    for hour, price, volume in points:
        ts = T0 + timedelta(hours=hour)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=volume,
            buys=10,
            sells=5,
        )
    return asset, pool, source


def _seed_depth(session, *, pool_id: int, source_id: int, depths: list[tuple[int, float]]):
    for hour, depth in depths:
        ts = T0 + timedelta(hours=hour)
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool_id,
            source_id=source_id,
            ts=ts,
            observed_at=ts,
            reserve_usd=depth,
        )


def _label_value(session, asset_id: int, ts: datetime, label_type: str) -> str | None:
    row = session.scalar(
        select(models.Label).where(
            models.Label.asset_id == asset_id,
            models.Label.ts == ts,
            models.Label.label_type == label_type,
        )
    )
    return row.label_value if row is not None else None


def _pump_points() -> list[tuple[int, float, float]]:
    # Price 2.00 at +5h, 1.00 elsewhere; trailing volume 1_000 throughout.
    return [(h, 2.0 if h == 5 else 1.0, 1_000) for h in range(25)]


def test_generate_writes_exit_window_labels_beside_harvest(session):
    # Constant depth 10_000: every size's executable peak is the +5h
    # row (price 2.00, depth and volume at peak there). Cost leg:
    # s02 1.96% and s10 9.09% inside the 10% cap; s25 20% over it.
    asset, pool, source = _seed_asset(session, symbol="EWIN", points=_pump_points())
    _seed_depth(
        session, pool_id=pool.id, source_id=source.id, depths=[(h, 10_000) for h in range(25)]
    )
    session.commit()
    counts = LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_EXECUTABLE_PEAK_GAIN_S10) == "0.818182"
    assert _label_value(session, asset.id, T0, LABEL_EXIT_WINDOW_FEASIBLE_S02) == "1"
    assert _label_value(session, asset.id, T0, LABEL_EXIT_WINDOW_FEASIBLE_S10) == "1"
    assert _label_value(session, asset.id, T0, LABEL_EXIT_WINDOW_FEASIBLE_S25) == "0"
    assert counts["exit_window"] >= 3


def test_generate_without_depth_writes_no_exit_window_labels(session):
    # No depth path -> no exit-window labels (absence, not zeros),
    # exactly as the Task 7 harvest labels behave.
    asset, _pool, _source = _seed_asset(session, symbol="EWNONE", points=_pump_points())
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    for label_type in (
        LABEL_EXIT_WINDOW_FEASIBLE_S02,
        LABEL_EXIT_WINDOW_FEASIBLE_S10,
        LABEL_EXIT_WINDOW_FEASIBLE_S25,
    ):
        assert _label_value(session, asset.id, T0, label_type) is None


def test_exit_window_labels_point_in_time_under_restatement(session):
    # A depth print restated AFTER the generation run (observed_at
    # beyond the run's decision_ts) must not move the label: had the
    # restated 1_000_000 print at +5h leaked in, D_peak would be
    # 1_000_000, the 50% depth floor 500_000, and every size would
    # read "0". The point-in-time gate keeps the original episode.
    # (The restatement arrives under a second source: the table's
    # (pool, ts, source) uniqueness forbids a same-source twin, and
    # the depth series is source-agnostic — observed_at is the gate.)
    asset, pool, source = _seed_asset(session, symbol="EWPIT", points=_pump_points())
    _seed_depth(
        session, pool_id=pool.id, source_id=source.id, depths=[(h, 10_000) for h in range(25)]
    )
    generation_ts = T0 + timedelta(hours=48)
    restating_source = get_or_create_source(
        session,
        name="geckoterminal",
        source_type="market_data",
        tier="venue",
        base_url="https://api.geckoterminal.com",
    )
    session.add(
        models.LiquiditySnapshot(
            pool_id=pool.id,
            source_id=restating_source.id,
            ts=T0 + timedelta(hours=5),
            observed_at=generation_ts + timedelta(hours=1),
            reserve_usd=1_000_000,
        )
    )
    session.commit()
    LabelEngine().generate(session, decision_ts=generation_ts)
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_EXIT_WINDOW_FEASIBLE_S02) == "1"
    assert _label_value(session, asset.id, T0, LABEL_EXIT_WINDOW_FEASIBLE_S10) == "1"
    assert _label_value(session, asset.id, T0, LABEL_EXIT_WINDOW_FEASIBLE_S25) == "0"


def test_bootstrap_writes_exit_window_labels(session):
    asset, pool, source = _seed_asset(session, symbol="EWBOOT", points=_pump_points())
    _seed_depth(
        session, pool_id=pool.id, source_id=source.id, depths=[(h, 10_000) for h in range(25)]
    )
    upsert_feature(
        session,
        asset_id=asset.id,
        decision_ts=T0,
        feature_name="liquidity_depth",
        feature_value=200_000.0,
        source_count=1,
        freshness_score=1.0,
        missing_flag=False,
    )
    session.commit()
    counts = seed_labels_at_feature_timestamps(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_EXIT_WINDOW_FEASIBLE_S02) == "1"
    assert _label_value(session, asset.id, T0, LABEL_EXIT_WINDOW_FEASIBLE_S10) == "1"
    assert _label_value(session, asset.id, T0, LABEL_EXIT_WINDOW_FEASIBLE_S25) == "0"
    assert counts["exit_window"] == 3


# ── the cohort-conditional second stage ──────────────────────────────────────


def _seed_cohort_asset(
    session,
    *,
    symbol: str,
    phase: float | None,
    ignition: str,
    peak_hour: int | None,
):
    """One labeled decision point at T0 for the cohort fit: features
    (lifecycle_phase unless None) + ignition/collapse labels at T0,
    and an hourly price arc whose peak sits at peak_hour."""
    points = [(h, 2.0 if h == peak_hour else 1.0, 1_000) for h in range(25)]
    asset, _pool, _source = _seed_asset(session, symbol=symbol, points=points)
    if phase is not None:
        upsert_feature(
            session,
            asset_id=asset.id,
            decision_ts=T0,
            feature_name="lifecycle_phase",
            feature_value=phase,
            source_count=1,
            freshness_score=1.0,
            missing_flag=False,
        )
    else:
        upsert_feature(
            session,
            asset_id=asset.id,
            decision_ts=T0,
            feature_name="liquidity_depth",
            feature_value=200_000.0,
            source_count=1,
            freshness_score=1.0,
            missing_flag=False,
        )
    observed = T0 + timedelta(hours=1)
    upsert_label(
        session,
        asset_id=asset.id,
        ts=T0,
        label_type=LABEL_IGNITION,
        label_value=ignition,
        decision_ts=observed,
        source="forecast:test",
    )
    upsert_label(
        session,
        asset_id=asset.id,
        ts=T0,
        label_type=LABEL_COLLAPSE,
        label_value="0",
        decision_ts=observed,
        source="forecast:test",
    )
    return asset


def test_cohort_hazard_fit_excludes_pre_ignition_rows(session):
    # IGNITED cohort: a PARABOLIC decision point whose asset pumped
    # again (peak at +3h) and an IGNITION point that did not (censored).
    _seed_cohort_asset(session, symbol="CPUMP", phase=2.0, ignition="1", peak_hour=3)
    _seed_cohort_asset(session, symbol="CFLAT", phase=1.0, ignition="0", peak_hour=None)
    # Pre-ignition rows that must NOT enter the fit: a SEEDING point
    # whose asset pumped later (the contamination this stage removes)
    # and a point with no phase feature at all (SEEDING by definition).
    _seed_cohort_asset(session, symbol="CSEED", phase=0.0, ignition="1", peak_hour=3)
    _seed_cohort_asset(session, symbol="CNONE", phase=None, ignition="1", peak_hour=3)
    session.commit()

    model = fit_cohort_peak_hazards(session, settings=get_settings())
    # Only the two post-ignition phases appear, and the fitted cohort
    # n equals the ignited-cohort n exactly — no seeding mass at all.
    assert set(model) == {"parabolic", "ignition"}
    assert sum(fit.at_risk_count for fit in model.values()) == 2
    assert model["parabolic"].event_count == 1
    assert model["parabolic"].mean_time_to_peak == pytest.approx(2.5)
    # Non-ignited phases receive no second-stage score.
    assert harvest_window_closing_hazard(model, phase="seeding", hours_in_position=1.0) == 0.0
    assert harvest_window_closing_hazard(model, phase="terminal", hours_in_position=1.0) == 0.0
    # The parabolic curve is (1, 1, 1, 0, ...): the peak lands in the
    # (2h, 3h] bin, so the closing hazard is 0 at hour 0 and 1 at hour 2.
    assert harvest_window_closing_hazard(model, phase="parabolic", hours_in_position=0.0) == 0.0
    assert harvest_window_closing_hazard(model, phase="parabolic", hours_in_position=2.0) == 1.0


def test_closing_hazard_reads_survival_curve_bins():
    fit = HazardFit(
        mean_time_to_collapse=5.0,
        event_count=10,
        at_risk_count=20,
        curve=(1.0, 0.8, 0.5, 0.5),
    )
    model = {"saturation": fit}
    # Bin hazard (S(h) - S(h+1)) / S(h): hour 0 -> 0.2, hour 1 -> 0.375,
    # hour 2 -> 0.0; beyond the fitted window the last bin repeats.
    assert harvest_window_closing_hazard(model, phase="saturation", hours_in_position=0.0) == (
        pytest.approx(0.2)
    )
    assert harvest_window_closing_hazard(model, phase="saturation", hours_in_position=1.0) == (
        pytest.approx(0.375)
    )
    assert harvest_window_closing_hazard(model, phase="saturation", hours_in_position=2.0) == 0.0
    assert harvest_window_closing_hazard(model, phase="saturation", hours_in_position=99.0) == 0.0
    # A phase with no fit, and a fit with no curve, score zero.
    assert harvest_window_closing_hazard(model, phase="ignition", hours_in_position=0.0) == 0.0
    empty = {"ignition": HazardFit(None, 0, 3)}
    assert harvest_window_closing_hazard(empty, phase="ignition", hours_in_position=0.0) == 0.0
    # Survival already exhausted: the peak has certainly passed.
    dead = {"collapse": HazardFit(1.0, 5, 5, curve=(1.0, 0.0, 0.0))}
    assert harvest_window_closing_hazard(dead, phase="collapse", hours_in_position=1.0) == 1.0
