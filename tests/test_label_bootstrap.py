"""Tests for Phase 2: feature-aligned label bootstrapping.

Verifies that seed_labels_at_feature_timestamps generates labels at the
exact timestamps where features exist, bridging the gap that prevented
ML training.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from forecast.labels import (
    LABEL_COLLAPSE,
    LABEL_IGNITION,
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


def _seed_asset_with_features_and_snapshots(
    session, *, symbol: str, prices: list[float], feature_hours: list[int]
) -> models.Asset:
    """Create an asset with market snapshots and features at specified hours."""
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
    # Market snapshots at every hour
    for hour, price in enumerate(prices):
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
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            reserve_usd=200_000,
        )
    # Features at specified hours only (the gap the bootstrap fills)
    for hour in feature_hours:
        ts = T0 + timedelta(hours=hour)
        upsert_feature(
            session,
            asset_id=asset.id,
            decision_ts=ts,
            feature_name="liquidity_depth",
            feature_value=200_000.0,
            source_count=1,
            freshness_score=1.0,
            missing_flag=False,
        )
    return asset


def test_seed_labels_generates_labels_at_feature_timestamps(session) -> None:
    """Labels are generated at every (asset_id, ts) pair where features exist."""
    # Asset with 49h of prices, features only at hours 11 and 13
    asset = _seed_asset_with_features_and_snapshots(
        session,
        symbol="TEST",
        prices=[1.0] * 49,
        feature_hours=[11, 13],
    )
    session.commit()

    decision = T0 + timedelta(hours=48)
    counts = seed_labels_at_feature_timestamps(session, decision_ts=decision)
    session.commit()

    # Should have generated labels at hours 11 and 13
    assert counts["decision_points"] == 2
    # Check labels exist at the feature timestamps
    labels = session.scalars(
        select(models.Label).where(models.Label.asset_id == asset.id)
    ).all()
    assert len(labels) >= 4  # 2 timestamps × 2 label types (ignition + collapse)
    label_timestamps = {
        label.ts.replace(tzinfo=None) if label.ts.tzinfo else label.ts for label in labels
    }
    assert T0 + timedelta(hours=11) in label_timestamps
    assert T0 + timedelta(hours=13) in label_timestamps


def test_seed_labels_is_idempotent(session) -> None:
    """Calling seed_labels_at_feature_timestamps twice doesn't duplicate labels."""
    asset = _seed_asset_with_features_and_snapshots(
        session,
        symbol="IDEMP",
        prices=[1.0] * 49,
        feature_hours=[11],
    )
    session.commit()

    decision = T0 + timedelta(hours=48)

    # First call
    counts1 = seed_labels_at_feature_timestamps(session, decision_ts=decision)
    session.commit()
    assert counts1["decision_points"] == 1

    # Count labels after first call
    labels_after_1 = session.scalars(
        select(models.Label).where(models.Label.asset_id == asset.id)
    ).all()
    count_after_1 = len(labels_after_1)

    # Second call — should not create new labels
    counts2 = seed_labels_at_feature_timestamps(session, decision_ts=decision)
    session.commit()
    assert counts2["decision_points"] == 0  # Already has labels

    labels_after_2 = session.scalars(
        select(models.Label).where(models.Label.asset_id == asset.id)
    ).all()
    assert len(labels_after_2) == count_after_1


def test_seed_labels_skips_forward_window_not_elapsed(session) -> None:
    """Labels are not generated if the forward window hasn't elapsed."""
    asset = _seed_asset_with_features_and_snapshots(
        session,
        symbol="FUTURE",
        prices=[1.0] * 10,
        feature_hours=[5],
    )
    session.commit()

    # Decision time is only 6 hours after T0 — forward window (24h) hasn't elapsed
    decision = T0 + timedelta(hours=6)
    counts = seed_labels_at_feature_timestamps(session, decision_ts=decision)
    session.commit()

    assert counts["decision_points"] == 0
    labels = session.scalars(
        select(models.Label).where(models.Label.asset_id == asset.id)
    ).all()
    assert len(labels) == 0


def test_seed_labels_classifies_correctly(session) -> None:
    """Ignition and collapse labels are assigned based on price movement thresholds."""
    # Flat prices → no ignition, no collapse
    flat = _seed_asset_with_features_and_snapshots(
        session,
        symbol="FLAT",
        prices=[1.0] * 49,
        feature_hours=[11],
    )
    # Crash prices → collapse (feature at hour 11, crash starts at hour 12)
    crash = _seed_asset_with_features_and_snapshots(
        session,
        symbol="CRASH",
        prices=[1.0] * 12 + [0.2] * 37,
        feature_hours=[11],
    )
    # Pump prices → ignition (feature at hour 11, pump starts at hour 12)
    pump = _seed_asset_with_features_and_snapshots(
        session,
        symbol="PUMP",
        prices=[1.0] * 12 + [2.0] * 37,
        feature_hours=[11],
    )
    session.commit()

    decision = T0 + timedelta(hours=48)
    counts = seed_labels_at_feature_timestamps(session, decision_ts=decision)
    session.commit()

    # Should have generated labels for all three assets
    assert counts["decision_points"] == 3
    assert counts["ignition"] >= 1  # At least PUMP triggers ignition
    assert counts["collapse"] >= 1  # At least CRASH triggers collapse

    # FLAT: no ignition, no collapse (prices don't move 20% or drop 70%)
    flat_labels = session.scalars(
        select(models.Label).where(models.Label.asset_id == flat.id)
    ).all()
    flat_by_type = {row.label_type: row.label_value for row in flat_labels}
    assert flat_by_type.get(LABEL_IGNITION) == "0"
    assert flat_by_type.get(LABEL_COLLAPSE) == "0"

    # CRASH: collapse = 1 (drops from 1.0 to 0.2 = -80%)
    crash_labels = session.scalars(
        select(models.Label).where(models.Label.asset_id == crash.id)
    ).all()
    crash_by_type = {row.label_type: row.label_value for row in crash_labels}
    assert crash_by_type.get(LABEL_COLLAPSE) == "1"

    # PUMP: ignition = 1 (rises from 1.0 to 2.0 = +100%)
    pump_labels = session.scalars(
        select(models.Label).where(models.Label.asset_id == pump.id)
    ).all()
    pump_by_type = {row.label_type: row.label_value for row in pump_labels}
    assert pump_by_type.get(LABEL_IGNITION) == "1"


def test_seed_labels_records_health(session) -> None:
    """Health row is recorded after bootstrap label generation."""
    _seed_asset_with_features_and_snapshots(
        session,
        symbol="HEALTH",
        prices=[1.0] * 49,
        feature_hours=[11],
    )
    session.commit()

    decision = T0 + timedelta(hours=48)
    seed_labels_at_feature_timestamps(session, decision_ts=decision)
    session.commit()

    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "forecast_labels_bootstrap")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert health is not None
    assert health.state == "ok"


def test_seed_labels_handles_empty_database(session) -> None:
    """Function handles the case where no features exist gracefully."""
    decision = T0 + timedelta(hours=48)
    counts = seed_labels_at_feature_timestamps(session, decision_ts=decision)
    session.commit()

    assert counts == {"ignition": 0, "collapse": 0, "decision_points": 0}
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "forecast_labels_bootstrap")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert health is not None
    assert health.state == "ok"
