"""Ticket 1, step 2 — staleness guard (mechanism M1).

``compute_market_block`` used to present the latest snapshot at-or-before the
decision time as current no matter how old it was: with a once-per-24h
refresh, a token mid-collapse scored on its pre-collapse snapshot and sat
GREEN on dead data. The guard marks the market/liquidity blocks *missing*
when their latest snapshot is older than the configured bound at decision
time, so stale tokens flow into the existing missing-data machinery
(unknown-liquidity points, defaulted inputs) instead. The same treatment
applies to ``_forecast_probability``: a forecast older than its bound is
unknown, not a frozen probability carried forward forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from features.factory import FeatureFactory, compute_market_block
from storage import models
from tests.conftest import seed_market_asset

DECISION = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

MARKET_FEATURES = (
    "five_min_return",
    "one_hour_return",
    "volume_acceleration",
    "buy_sell_ratio",
    "unique_buyers_estimate",
    "volatility",
    "venue_agreement",
    "drawdown_from_24h_peak",
)
LIQUIDITY_FEATURES = ("liquidity_depth", "liquidity_change", "spread_estimate")


def _market_row(ts: datetime, *, price: float = 1.0, pair_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        ts=ts,
        observed_at=ts,
        source_id=1,
        pair_id=pair_id,
        price_usd=price,
        volume_usd=100.0,
        buys=10,
        sells=5,
    )


def _liquidity_row(ts: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        ts=ts,
        observed_at=ts,
        source_id=1,
        reserve_usd=50_000.0,
    )


def _pair(created: datetime) -> SimpleNamespace:
    return SimpleNamespace(created_at=created, created_at_source=created)


def _asset() -> SimpleNamespace:
    return SimpleNamespace(first_seen_at=DECISION - timedelta(hours=6))


def test_market_block_fresh_snapshots_stay_present() -> None:
    rows = [
        _market_row(DECISION - timedelta(minutes=65), price=0.9),
        _market_row(DECISION - timedelta(minutes=30), price=1.0),
        _market_row(DECISION - timedelta(minutes=10), price=1.1),
    ]
    values = compute_market_block(
        asset=_asset(),
        pairs=[_pair(DECISION - timedelta(hours=6))],
        market_rows=rows,
        liquidity_rows=[
            _liquidity_row(DECISION - timedelta(minutes=90)),
            _liquidity_row(DECISION - timedelta(minutes=30)),
        ],
        decision_ts=DECISION,
    )
    for name in MARKET_FEATURES:
        assert values[name].missing is False, name
    for name in LIQUIDITY_FEATURES:
        assert values[name].missing is False, name


def test_market_block_stale_snapshots_read_missing() -> None:
    rows = [
        _market_row(DECISION - timedelta(hours=5), price=0.9),
        _market_row(DECISION - timedelta(hours=4), price=1.0),
        _market_row(DECISION - timedelta(hours=3), price=1.1),
    ]
    values = compute_market_block(
        asset=_asset(),
        pairs=[_pair(DECISION - timedelta(hours=6))],
        market_rows=rows,
        liquidity_rows=[
            _liquidity_row(DECISION - timedelta(minutes=90)),
            _liquidity_row(DECISION - timedelta(minutes=30)),
        ],
        decision_ts=DECISION,
    )
    for name in MARKET_FEATURES:
        assert values[name].missing is True, name
    # Pair age is a fact about the pair's creation, not a market
    # measurement — staleness of snapshots must not erase it.
    assert values["pair_age_minutes"].missing is False
    assert values["pair_age_minutes"].value == 360.0
    # Fresh liquidity is unaffected by stale market data.
    for name in LIQUIDITY_FEATURES:
        assert values[name].missing is False, name


def test_market_block_stale_liquidity_reads_missing() -> None:
    rows = [
        _market_row(DECISION - timedelta(minutes=65), price=0.9),
        _market_row(DECISION - timedelta(minutes=30), price=1.0),
        _market_row(DECISION - timedelta(minutes=10), price=1.1),
    ]
    values = compute_market_block(
        asset=_asset(),
        pairs=[_pair(DECISION - timedelta(hours=6))],
        market_rows=rows,
        liquidity_rows=[
            _liquidity_row(DECISION - timedelta(hours=6)),
            _liquidity_row(DECISION - timedelta(hours=5)),
        ],
        decision_ts=DECISION,
    )
    for name in LIQUIDITY_FEATURES:
        assert values[name].missing is True, name
    for name in MARKET_FEATURES:
        assert values[name].missing is False, name


def test_market_block_snapshot_exactly_at_bound_is_not_stale() -> None:
    rows = [_market_row(DECISION - timedelta(hours=2), price=1.0)]
    values = compute_market_block(
        asset=_asset(),
        pairs=[],
        market_rows=rows,
        liquidity_rows=[],
        decision_ts=DECISION,
    )
    assert values["buy_sell_ratio"].missing is False


def test_forecast_probability_fresh_is_returned(session) -> None:
    asset = seed_market_asset(session)
    session.add(
        models.Forecast(
            asset_id=asset.id,
            decision_ts=DECISION - timedelta(hours=1),
            observed_at=DECISION - timedelta(hours=1),
            p_ignition_24h=0.1,
            p_collapse_24h=0.42,
            details={},
            model_version="test-v1",
        )
    )
    session.commit()

    assert FeatureFactory()._forecast_probability(session, asset.id, DECISION) == 0.42


def test_forecast_probability_stale_reads_unknown(session) -> None:
    asset = seed_market_asset(session)
    session.add(
        models.Forecast(
            asset_id=asset.id,
            decision_ts=DECISION - timedelta(hours=30),
            observed_at=DECISION - timedelta(hours=30),
            p_ignition_24h=0.1,
            p_collapse_24h=0.9,
            details={},
            model_version="test-v1",
        )
    )
    session.commit()

    assert FeatureFactory()._forecast_probability(session, asset.id, DECISION) is None
