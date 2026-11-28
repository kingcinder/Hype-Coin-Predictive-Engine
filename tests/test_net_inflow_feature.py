"""Net-inflow feature tests (mission-refactor Task 5).

``net_inflow_ratio`` is the reserve-growth feature: the relative change in
a pool's quote reserves over ``feature_net_inflow_lookback_hours`` (1h by
default), point-in-time. On AMM pools the reserve trajectory IS the curve
velocity — net quote inflow is mechanically hard to fake, unlike volume.
``reserve_quote`` is the primary series; where it is null the feature
falls back to ``reserve_usd`` growth. With no prior print (or a
non-positive prior reserve) the feature is missing, never a fabricated 0.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from features.factory import FeatureFactory, FeatureValue
from storage import models
from storage.repository import (
    insert_liquidity_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def _seed_pool(session) -> tuple[models.Asset, models.Pool, models.Source]:
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Inflow111111111111111111111111111111111111",
        symbol="INFLOW",
        name="Inflow Fixture",
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
        pair_address="InflowPair11111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0 - timedelta(days=1),
    )
    pool.created_at = T0 - timedelta(days=1)
    pair.created_at = T0 - timedelta(days=1)
    session.commit()
    return asset, pool, source


def _print(
    session,
    pool: models.Pool,
    source: models.Source,
    *,
    ts: datetime,
    observed_at: datetime,
    reserve_quote: float | None = None,
    reserve_usd: float | None = None,
) -> None:
    insert_liquidity_snapshot_once(
        session,
        pool_id=pool.id,
        source_id=source.id,
        ts=ts,
        observed_at=observed_at,
        reserve_usd=reserve_usd,
        reserve_quote=reserve_quote,
    )
    session.commit()


def _features(session, asset: models.Asset, decision_ts: datetime) -> dict[str, FeatureValue]:
    values = FeatureFactory().build_for_asset(session, asset, decision_ts)
    return {value.name: value for value in values}


def test_net_inflow_ratio_from_quote_reserves(session):
    # reserve_quote 100 -> 150 over the lookback: ratio +0.5, point-in-time
    # (a later reserve print must not change the value at decision_ts).
    asset, pool, source = _seed_pool(session)
    _print(session, pool, source, ts=T0 - 2 * H, observed_at=T0 - 2 * H, reserve_quote=100.0)
    _print(session, pool, source, ts=T0 - 10 * M, observed_at=T0 - 10 * M, reserve_quote=150.0)
    # Leak attempts: a print after the decision, and a restated print whose
    # ts precedes the decision but which was only observed after it.
    _print(session, pool, source, ts=T0 + H, observed_at=T0 + H, reserve_quote=999.0)
    _print(session, pool, source, ts=T0 - 5 * M, observed_at=T0 + 2 * H, reserve_quote=5000.0)

    feats = _features(session, asset, T0)
    assert feats["net_inflow_ratio"].missing is False
    assert feats["net_inflow_ratio"].value == pytest.approx(0.5)


def test_net_inflow_missing_without_prior_print(session):
    # A single current print has no prior reserve to grow from: missing.
    asset, pool, source = _seed_pool(session)
    _print(session, pool, source, ts=T0 - 10 * M, observed_at=T0 - 10 * M, reserve_quote=150.0)

    feats = _features(session, asset, T0)
    assert feats["net_inflow_ratio"].missing is True


def test_net_inflow_falls_back_to_reserve_usd(session):
    # reserve_quote null on every print: reserve_usd growth carries it.
    asset, pool, source = _seed_pool(session)
    _print(session, pool, source, ts=T0 - 2 * H, observed_at=T0 - 2 * H, reserve_usd=200.0)
    _print(session, pool, source, ts=T0 - 10 * M, observed_at=T0 - 10 * M, reserve_usd=300.0)

    feats = _features(session, asset, T0)
    assert feats["net_inflow_ratio"].missing is False
    assert feats["net_inflow_ratio"].value == pytest.approx(0.5)


def test_net_inflow_missing_when_prior_reserve_nonpositive(session):
    # Growth relative to a zero prior reserve is undefined: missing, and
    # the (null) reserve_usd fallback cannot rescue it.
    asset, pool, source = _seed_pool(session)
    _print(session, pool, source, ts=T0 - 2 * H, observed_at=T0 - 2 * H, reserve_quote=0.0)
    _print(session, pool, source, ts=T0 - 10 * M, observed_at=T0 - 10 * M, reserve_quote=150.0)

    feats = _features(session, asset, T0)
    assert feats["net_inflow_ratio"].missing is True
