"""Buyer-acceleration feature tests (mission-refactor Task 14).

``unique_buyers_acceleration`` is the second difference of the
``unique_buyers_estimate`` series over the factory's snapshot grid —
spec §3 item 2's actual signal (Task 5's composite ranks the *level*).
Buyer growth that is speeding up reads positive; steady linear growth
reads zero. With fewer than three buyer prints the feature is missing,
never a fabricated 0.

Wash discipline: a cohort row flagged by ``wash_lite_adjust`` must not
be able to buy an acceleration rank — ``rank_entry_cohort`` drops the
acceleration term for flagged rows (percentile 0.0) and records the
drop in ``components`` as ``unique_buyers_acceleration_wash_dropped``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.config import get_settings
from features.factory import FeatureFactory, FeatureValue
from scoring.entry_rank import ENTRY_RANK_COMPONENTS, rank_entry_cohort
from storage import models
from storage.repository import (
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)


def _seed_pair(session) -> tuple[models.Asset, models.Pair, models.Source]:
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Accel111111111111111111111111111111111111",
        symbol="ACCEL",
        name="Accel Fixture",
        first_seen_at=T0 - timedelta(days=1),
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="USDC111111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=T0 - timedelta(days=365),
    )
    pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address="AccelPair111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0 - timedelta(days=1),
    )
    pool.created_at = T0 - timedelta(days=1)
    pair.created_at = T0 - timedelta(days=1)
    session.commit()
    return asset, pair, source


def _snapshot(
    session,
    pair: models.Pair,
    source: models.Source,
    *,
    ts: datetime,
    buys: int,
    observed_at: datetime | None = None,
) -> None:
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=ts,
        observed_at=observed_at if observed_at is not None else ts,
        price_usd=1.0,
        volume_usd=10_000.0,
        buys=buys,
        sells=50,
    )
    session.commit()


def _features(session, asset: models.Asset, decision_ts: datetime) -> dict[str, FeatureValue]:
    values = FeatureFactory().build_for_asset(session, asset, decision_ts)
    return {value.name: value for value in values}


# ---------------------------------------------------------------------------
# factory: second difference of the buyer estimate
# ---------------------------------------------------------------------------


def test_acceleration_zero_for_linear_buyer_growth(session):
    # Buyers 100, 150, 200 at hourly snapshots: first differences
    # +50, +50 -> acceleration 0.
    asset, pair, source = _seed_pair(session)
    _snapshot(session, pair, source, ts=T0 - 2 * H, buys=100)
    _snapshot(session, pair, source, ts=T0 - 1 * H, buys=150)
    _snapshot(session, pair, source, ts=T0, buys=200)

    feats = _features(session, asset, T0)
    assert feats["unique_buyers_acceleration"].missing is False
    assert feats["unique_buyers_acceleration"].value == pytest.approx(0.0)


def test_acceleration_positive_for_convex_growth(session):
    # Buyers 100, 120, 200: differences +20, +80 -> acceleration +60/h.
    asset, pair, source = _seed_pair(session)
    _snapshot(session, pair, source, ts=T0 - 2 * H, buys=100)
    _snapshot(session, pair, source, ts=T0 - 1 * H, buys=120)
    _snapshot(session, pair, source, ts=T0, buys=200)

    feats = _features(session, asset, T0)
    assert feats["unique_buyers_acceleration"].missing is False
    assert feats["unique_buyers_acceleration"].value == pytest.approx(60.0)


def test_acceleration_missing_with_fewer_than_three_points(session):
    # Two buyer prints give a level and one difference — no curvature.
    # The feature must read missing, not a fabricated 0.
    asset, pair, source = _seed_pair(session)
    _snapshot(session, pair, source, ts=T0 - 1 * H, buys=120)
    _snapshot(session, pair, source, ts=T0, buys=200)

    feats = _features(session, asset, T0)
    assert feats["unique_buyers_acceleration"].missing is True


def test_acceleration_ignores_restated_snapshot(session):
    # A restated print whose ts precedes the decision but which was only
    # observed after it must not enter the series (point-in-time): with
    # the leak, the three most recent points would include the wild
    # print and the acceleration would explode.
    asset, pair, source = _seed_pair(session)
    _snapshot(session, pair, source, ts=T0 - 2 * H, buys=100)
    _snapshot(session, pair, source, ts=T0 - 1 * H, buys=120)
    _snapshot(session, pair, source, ts=T0, buys=200)
    _snapshot(
        session,
        pair,
        source,
        ts=T0 - timedelta(minutes=30),
        buys=99_999,
        observed_at=T0 + 2 * H,
    )

    feats = _features(session, asset, T0)
    assert feats["unique_buyers_acceleration"].value == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# entry rank: wash-flagged rows drop the acceleration component
# ---------------------------------------------------------------------------


def test_wash_flagged_row_drops_acceleration_component():
    # Cohort row flagged by wash_lite_adjust with a huge acceleration
    # value: component percentile is 0.0 and the drop is recorded.
    # intensity = 400 / 10 = 40 > 20 threshold -> flagged.
    flagged_row = {
        "net_inflow_ratio": 0.1,
        "buy_sell_ratio": 3.0,
        "unique_buyers_estimate": 10.0,
        "liquidity_depth": 100.0,
        "unique_buyers_acceleration": 10_000.0,
        "volume_acceleration": 400.0,
    }
    clean_row = {
        "net_inflow_ratio": 0.1,
        "buy_sell_ratio": 2.5,
        "unique_buyers_estimate": 10.0,
        "liquidity_depth": 100.0,
        "unique_buyers_acceleration": 50.0,
        "volume_acceleration": 100.0,
    }
    candidates = rank_entry_cohort([(1, flagged_row), (2, clean_row)], settings=get_settings())
    by_id = {candidate.asset_id: candidate for candidate in candidates}

    assert by_id[1].wash_flagged is True
    assert by_id[1].components["unique_buyers_acceleration"] == 0.0
    assert by_id[1].components["unique_buyers_acceleration_wash_dropped"] == 1.0
    # The drop marker is a record, not a percentile: it never enters the sum.
    assert by_id[1].rank_sum == pytest.approx(
        sum(by_id[1].components[name] for name in ENTRY_RANK_COMPONENTS)
    )

    assert by_id[2].wash_flagged is False
    assert by_id[2].components["unique_buyers_acceleration"] > 0.0
    assert "unique_buyers_acceleration_wash_dropped" not in by_id[2].components
