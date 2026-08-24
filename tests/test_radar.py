from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from radar.ignition import IgnitionRadar
from storage import models
from storage.repository import insert_liquidity_snapshot_once, insert_market_snapshot_once
from tests.conftest import seed_market_asset

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_first_liquidity_injection_and_sniper_burst_are_idempotent(session) -> None:
    asset = seed_market_asset(session)
    radar = IgnitionRadar()
    counts = radar.scan(session, decision_ts=NOW)
    session.commit()
    assert counts["first_liquidity_injection"] == 1
    assert counts["sniper_burst"] == 1

    events = session.scalars(select(models.IgnitionEvent)).all()
    assert {event.event_type for event in events} == {
        "first_liquidity_injection",
        "sniper_burst",
    }

    radar.scan(session, decision_ts=NOW)
    session.commit()
    assert session.scalar(select(func.count()).select_from(models.IgnitionEvent)) == 2
    alerts = session.scalars(select(models.Alert)).all()
    assert {alert.alert_type for alert in alerts} == {"ignition_detected"}
    assert {alert.asset_id for alert in alerts} == {asset.id}


def test_sniper_burst_requires_buy_volume_and_ratio(session) -> None:
    seed_market_asset(
        session,
        address="TokenQuiet111111111111111111111111111111",
        symbol="QUIET",
        pair_address="PairQuiet111111111111111111111111111111",
    )
    pair = session.scalar(
        select(models.Pair).where(models.Pair.base_asset_id == session.scalar(
            select(models.Asset.id).where(models.Asset.symbol == "QUIET")
        ))
    )
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=NOW - timedelta(minutes=30),
        observed_at=NOW - timedelta(minutes=30),
        price_usd=1.2,
        volume_usd=5_000,
        buys=2,
        sells=60,
        trades=62,
    )
    session.commit()
    counts = IgnitionRadar().scan(session, decision_ts=NOW)
    session.commit()
    assert counts["sniper_burst"] == 0
    assert counts["first_liquidity_injection"] == 1


def test_liquidity_withdrawal_detected_when_volume_cannot_explain_drop(session) -> None:
    seed_market_asset(
        session,
        address="TokenWithdraw1111111111111111111111111111",
        symbol="WDR",
        pair_address="PairWithdraw111111111111111111111111111",
    )
    pool = session.scalar(
        select(models.Pool).where(models.Pool.base_asset_id == session.scalar(
            select(models.Asset.id).where(models.Asset.symbol == "WDR")
        ))
    )
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    insert_liquidity_snapshot_once(
        session,
        pool_id=pool.id,
        source_id=source.id,
        ts=NOW + timedelta(hours=1),
        observed_at=NOW + timedelta(hours=1),
        reserve_usd=40_000,
    )
    session.commit()
    counts = IgnitionRadar().scan(session, decision_ts=NOW + timedelta(hours=2))
    session.commit()
    assert counts["liquidity_withdrawal"] == 1
    event = session.scalar(
        select(models.IgnitionEvent).where(
            models.IgnitionEvent.event_type == "liquidity_withdrawal"
        )
    )
    assert event is not None
    assert event.details["drop_pct"] > 50
    assert event.details["current_reserve_usd"] == 40_000
    alert = session.scalar(
        select(models.Alert).where(models.Alert.alert_type == "liquidity_withdrawal_warning")
    )
    assert alert is not None
    assert "WDR" in alert.message


def test_high_volume_window_masks_withdrawal(session) -> None:
    seed_market_asset(
        session,
        address="TokenMasked111111111111111111111111111111",
        symbol="MASK",
        pair_address="PairMasked111111111111111111111111111111",
    )
    asset = session.scalar(select(models.Asset).where(models.Asset.symbol == "MASK"))
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    pool = session.scalar(select(models.Pool).where(models.Pool.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=NOW + timedelta(minutes=30),
        observed_at=NOW + timedelta(minutes=30),
        price_usd=1.1,
        volume_usd=200_000,
        buys=300,
        sells=50,
        trades=350,
    )
    insert_liquidity_snapshot_once(
        session,
        pool_id=pool.id,
        source_id=source.id,
        ts=NOW + timedelta(hours=1),
        observed_at=NOW + timedelta(hours=1),
        reserve_usd=40_000,
    )
    session.commit()
    counts = IgnitionRadar().scan(session, decision_ts=NOW + timedelta(hours=2))
    session.commit()
    assert counts["liquidity_withdrawal"] == 0


def test_radar_health_recorded(session) -> None:
    seed_market_asset(session)
    IgnitionRadar().scan(session, decision_ts=NOW)
    session.commit()
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "radar")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert health is not None
    assert health.state == "ok"
