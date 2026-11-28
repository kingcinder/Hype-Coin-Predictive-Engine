"""Task 7: harvest labels — the executable episode, not the price episode.

A price peak you cannot sell into is not a peak. Harvest labels describe
the episode in executable value: each candidate sell is sized at a
reference fraction of the episode's PEAK quote depth and filled against
a constant-product pool (average fill price ``price * D / (D + N)``), so
the executable peak can sit BEFORE the price peak when the depth was
there and not at the thin top print. The harvest window additionally
requires depth >= 50% of peak depth — the back half of a price plateau
with the pool drained is not a harvest window. Collapse-confirmation is
a >= 50% drawdown FROM the peak price with no recovery to within 20% of
the peak inside 4h. A second leg (new EV peak > 1.5x after a >= 30%
drawdown) is flagged, never deleted or split away.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from common.config import get_settings
from forecast.harvest import harvest_path
from forecast.labels import (
    LABEL_COLLAPSE_CONFIRMED,
    LABEL_EXECUTABLE_PEAK_GAIN_S02,
    LABEL_EXECUTABLE_PEAK_GAIN_S10,
    LABEL_EXECUTABLE_PEAK_GAIN_S25,
    LABEL_FORWARD_MAX_GAIN,
    LABEL_HARVEST_WINDOW_START,
    LABEL_SECOND_LEG,
    LabelEngine,
    harvest_label_specs,
    seed_labels_at_feature_timestamps,
)
from storage import models
from storage.repository import (
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    upsert_asset,
    upsert_feature,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0)  # naive for SQLite compatibility
START = datetime(2026, 11, 20, tzinfo=UTC)


def _rows(points: list[tuple[float, float, float | None]]):
    return [(START + timedelta(hours=h), price, depth) for h, price, depth in points]


# ── the executable peak ──────────────────────────────────────────────────────


def test_executable_peak_haircuts_thin_peak():
    # Rows (price, quote depth USD), entry price 1.00:
    #   +0h (1.00, 2_000)   +1h (2.00, 10_000)
    #   +2h (3.00, 1_000)   +3h (2.20, 3_000)
    # Peak depth D_peak = 10_000 (at +1h), so the reference notionals
    # are fractions of PEAK depth: N(0.02)=200, N(0.10)=1_000,
    # N(0.25)=2_500. Exec prices price*D/(D+N) per row:
    #   f=0.02: 0.909 / 1.961 / 2.500 / 2.063 -> peak 2.500, gain 1.50
    #   f=0.10: 0.667 / 1.818 / 1.500 / 1.650 -> peak 1.818, gain 0.818
    #     (the 10% size's executable peak is at +1h, BEFORE the price
    #     peak — the depth was there, not at the thin 3.00 print)
    #   f=0.25: 0.444 / 1.600 / 0.857 / 1.200 -> peak 1.600, gain 0.60
    # Gains strictly decrease with reference size.
    rows = _rows([(0, 1.00, 2_000), (1, 2.00, 10_000), (2, 3.00, 1_000), (3, 2.20, 3_000)])
    path = harvest_path(1.00, START, rows, settings=get_settings())
    assert path is not None
    assert path.executable_peak_gain_pct[0.02] == pytest.approx(1.50)
    assert path.executable_peak_gain_pct[0.10] == pytest.approx(0.818, abs=0.001)
    assert path.executable_peak_gain_pct[0.25] == pytest.approx(0.60)
    gains = [path.executable_peak_gain_pct[f] for f in (0.02, 0.10, 0.25)]
    assert gains == sorted(gains, reverse=True)


def test_harvest_path_empty_rows_returns_none():
    assert harvest_path(1.00, START, [], settings=get_settings()) is None


def test_harvest_path_without_usable_depth_returns_none():
    rows = _rows([(0, 1.00, None), (1, 2.00, 0), (2, 3.00, -5)])
    assert harvest_path(1.00, START, rows, settings=get_settings()) is None


# ── the harvest window ───────────────────────────────────────────────────────


def test_harvest_window_opens_at_theta_below_peak():
    # theta = 0.125, window at the f=0.10 reference. Rows:
    #   +0h (1.00, 8_000)   +1h (1.60, 9_000)   +2h (2.60, 10_000)
    #   +2.5h (2.95, 4_000) +3h (3.00, 6_000)   +4h (2.90, 2_000)
    # D_peak = 10_000, N(0.10) = 1_000. EV (exec price) per row:
    #   0.889 / 1.440 / 2.364 / 2.360 / 2.571 / 1.933; EV_peak = 2.571
    #   at +3h; (1 - theta) * EV_peak = 0.875 * 2.571 = 2.250.
    # EV condition holds at +2h, +2.5h, +3h. Depth >= 50% of D_peak
    # (5_000) holds at +2h (10_000) and +3h (6_000) but FAILS at +2.5h
    # (4_000) — so the first maximal interval is [+2h] alone, and the
    # window opens at +2h. On EV alone the window would run unbroken
    # +2h -> +3h; the depth condition is what splits it.
    rows = _rows(
        [
            (0, 1.00, 8_000),
            (1, 1.60, 9_000),
            (2, 2.60, 10_000),
            (2.5, 2.95, 4_000),
            (3, 3.00, 6_000),
            (4, 2.90, 2_000),
        ]
    )
    path = harvest_path(1.00, START, rows, settings=get_settings())
    assert path is not None
    assert path.harvest_window_start_hours == pytest.approx(2.0)


def test_harvest_window_depth_leg_defers_open():
    # Same path as above but the +2h row's depth is also drained
    # (4_000 < 5_000): the window cannot open until depth returns at
    # +3h, even though EV was within theta from +2h on.
    rows = _rows(
        [
            (0, 1.00, 8_000),
            (1, 1.60, 9_000),
            (2, 2.60, 4_000),
            (2.5, 2.95, 4_000),
            (3, 3.00, 6_000),
            (4, 2.90, 2_000),
        ]
    )
    path = harvest_path(1.00, START, rows, settings=get_settings())
    assert path is not None
    assert path.harvest_window_start_hours == pytest.approx(3.0)


# ── collapse confirmation ────────────────────────────────────────────────────


def test_collapse_confirmation_requires_half_drawdown_from_peak():
    # Peak price 3.00 (entry 1.00). Confirmation = first drawdown of
    # >= 50% FROM PEAK (price <= 1.50) with no recovery to within 20%
    # of peak (>= 2.40) within the following 4h.
    # Fixture: +5h 1.40 (crossing #1), +7h 2.50 (recovery inside 4h ->
    # crossing #1 negated), +9h 1.30 (crossing #2), +10..13h all
    # <= 2.40 -> crossing #2 confirms.
    rows = _rows(
        [
            (0, 1.00, 10_000),
            (2, 3.00, 10_000),
            (5, 1.40, 10_000),
            (7, 2.50, 10_000),
            (9, 1.30, 10_000),
            (10, 1.20, 10_000),
            (11, 1.10, 10_000),
            (12, 1.20, 10_000),
            (13, 1.00, 10_000),
        ]
    )
    path = harvest_path(1.00, START, rows, settings=get_settings())
    assert path is not None
    assert path.collapse_confirmed is True


def test_collapse_not_confirmed_when_recovery_within_4h():
    # Only crossing #1 from the fixture above (dip to 1.40, back to
    # 2.50 two hours later, never re-crosses): not confirmed.
    rows = _rows([(0, 1.00, 10_000), (2, 3.00, 10_000), (5, 1.40, 10_000), (7, 2.50, 10_000)])
    path = harvest_path(1.00, START, rows, settings=get_settings())
    assert path is not None
    assert path.collapse_confirmed is False
    # A dip to 1.60 (46.7% drawdown) never crosses the 50% line at all.
    rows = _rows([(0, 1.00, 10_000), (2, 3.00, 10_000), (5, 1.60, 10_000), (9, 1.55, 10_000)])
    path = harvest_path(1.00, START, rows, settings=get_settings())
    assert path is not None
    assert path.collapse_confirmed is False


# ── second leg ───────────────────────────────────────────────────────────────


def test_second_leg_episode_retained_and_flagged():
    # Depth constant 10_000 (EV ratios track price ratios). Entry 1.00,
    # first EV peak at price 3.00 (+3h), drawdown to 2.00 at +6h
    # (-33.3% >= the 30% second-leg drawdown; above the 1.50 collapse
    # line, so no collapse crossing), then a new peak at 4.60 (+10h):
    # 4.60 / 3.00 = 1.533 > 1.5x the prior EV peak. ONE episode:
    # second_leg == "1", collapse_confirmed == "0", and all six
    # harvest labels present.
    rows = _rows([(0, 1.00, 10_000), (3, 3.00, 10_000), (6, 2.00, 10_000), (10, 4.60, 10_000)])
    path = harvest_path(1.00, START, rows, settings=get_settings())
    assert path is not None
    assert path.second_leg is True
    assert path.collapse_confirmed is False
    specs = dict(harvest_label_specs(path))
    assert set(specs) == {
        LABEL_EXECUTABLE_PEAK_GAIN_S02,
        LABEL_EXECUTABLE_PEAK_GAIN_S10,
        LABEL_EXECUTABLE_PEAK_GAIN_S25,
        LABEL_HARVEST_WINDOW_START,
        LABEL_COLLAPSE_CONFIRMED,
        LABEL_SECOND_LEG,
    }
    assert specs[LABEL_SECOND_LEG] == "1"
    assert specs[LABEL_COLLAPSE_CONFIRMED] == "0"


# ── engine wiring ────────────────────────────────────────────────────────────


def _seed_asset(session, *, symbol: str, points: list[tuple[int, float]]):
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
    for hour, price in points:
        ts = T0 + timedelta(hours=hour)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=1_000,
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


def test_generate_writes_harvest_labels_following_ev_not_spot(session):
    # Price peaks at +5h (3.00) on a THIN pool (depth 1_000); depth is
    # 10_000 everywhere else. The spot forward label says +200%; the
    # executable labels must follow EV: at the 10% reference size the
    # +5h fill is 3.00 * 1000 / (1000 + 1000) = 1.50 -> gain 0.50.
    points = [(h, 3.0 if h == 5 else 1.0) for h in range(25)]
    asset, pool, source = _seed_asset(session, symbol="HARV", points=points)
    _seed_depth(
        session,
        pool_id=pool.id,
        source_id=source.id,
        depths=[(h, 1_000 if h == 5 else 10_000) for h in range(25)],
    )
    session.commit()
    counts = LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_GAIN) == "2.000000"
    assert _label_value(session, asset.id, T0, LABEL_EXECUTABLE_PEAK_GAIN_S02) == "1.500000"
    assert _label_value(session, asset.id, T0, LABEL_EXECUTABLE_PEAK_GAIN_S10) == "0.500000"
    assert _label_value(session, asset.id, T0, LABEL_EXECUTABLE_PEAK_GAIN_S25) == "-0.142857"
    # The only EV-qualifying row (+5h) fails the depth leg (1_000 <
    # 50% of the 10_000 peak depth), so no harvest window opens.
    assert _label_value(session, asset.id, T0, LABEL_HARVEST_WINDOW_START) is None
    # Post-peak the price sits at 1.00, a >50% drawdown from 3.00 with
    # no recovery: collapse confirmed. No second leg.
    assert _label_value(session, asset.id, T0, LABEL_COLLAPSE_CONFIRMED) == "1"
    assert _label_value(session, asset.id, T0, LABEL_SECOND_LEG) == "0"
    assert counts["harvest"] >= 5


def test_generate_without_depth_writes_no_harvest_labels(session):
    # No liquidity snapshots at all: no depth path -> no harvest labels
    # for the point (absence, not zeros), while forward labels land.
    asset, _pool, _source = _seed_asset(
        session,
        symbol="NODEPTH",
        points=[(h, 1.5 if h == 5 else 1.0) for h in range(30)],
    )
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_GAIN) == "0.500000"
    for label_type in (
        LABEL_EXECUTABLE_PEAK_GAIN_S02,
        LABEL_EXECUTABLE_PEAK_GAIN_S10,
        LABEL_EXECUTABLE_PEAK_GAIN_S25,
        LABEL_HARVEST_WINDOW_START,
        LABEL_COLLAPSE_CONFIRMED,
        LABEL_SECOND_LEG,
    ):
        assert _label_value(session, asset.id, T0, label_type) is None


def test_bootstrap_writes_harvest_labels(session):
    # Constant depth 10_000, price peak 1.50 at +5h: at the 10%
    # reference the executable peak is 1.50 * 10000 / 11000 = 1.3636
    # (gain 0.363636) and the harvest window opens at that row.
    asset, pool, source = _seed_asset(
        session,
        symbol="BOOTH",
        points=[(h, 1.5 if h == 5 else 1.0) for h in range(30)],
    )
    _seed_depth(
        session, pool_id=pool.id, source_id=source.id, depths=[(h, 10_000) for h in range(30)]
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
    assert _label_value(session, asset.id, T0, LABEL_EXECUTABLE_PEAK_GAIN_S10) == "0.363636"
    assert _label_value(session, asset.id, T0, LABEL_HARVEST_WINDOW_START) == "5.000000"
    assert _label_value(session, asset.id, T0, LABEL_COLLAPSE_CONFIRMED) == "0"
    assert _label_value(session, asset.id, T0, LABEL_SECOND_LEG) == "0"
    assert counts["harvest"] == 6
