from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from common.enums import IgnitionEventType, LifecyclePhase
from pump_physics.engine import (
    LifecycleEngine,
    PhaseEvidence,
    detect_phase,
    phase_rank,
)
from storage import models
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_market_snapshot_once,
    store_raw_evidence,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_market_asset, seed_reference

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _evidence(**overrides: Any) -> PhaseEvidence:
    values: dict[str, Any] = dict(
        has_pool=True,
        pool_age_hours=10.0,
        ignition_events=0,
        withdrawal_events=0,
        liquidity_usd=100_000.0,
        volume_acceleration=1.0,
        buy_sell_ratio=1.0,
        holder_growth=0.0,
        one_hour_return=5.0,
        narrative_velocity=1.0,
        last_trade_hours_ago=1.0,
        last_withdrawal_hours_ago=None,
    )
    values.update(overrides)
    return PhaseEvidence(**values)


def test_detect_phase_seeding_without_pool() -> None:
    assert detect_phase(_evidence(has_pool=False)) == LifecyclePhase.SEEDING


def test_detect_phase_rug_when_book_emptied() -> None:
    assert detect_phase(_evidence(withdrawal_events=1, liquidity_usd=0.0)) == LifecyclePhase.RUGGED


def test_detect_phase_dead_after_no_trades() -> None:
    assert detect_phase(_evidence(last_trade_hours_ago=200.0)) == LifecyclePhase.DEAD


def test_detect_phase_collapse_on_1h_crash() -> None:
    assert detect_phase(_evidence(one_hour_return=-30.0)) == LifecyclePhase.COLLAPSE


def test_detect_phase_collapse_on_withdrawal() -> None:
    assert (
        detect_phase(
            _evidence(
                withdrawal_events=2,
                liquidity_usd=50_000.0,
                last_withdrawal_hours_ago=1.0,
            )
        )
        == LifecyclePhase.COLLAPSE
    )


def test_detect_phase_ignores_stale_withdrawal() -> None:
    """A withdrawal from >48h ago is not a collapse signal (v3 rule)."""
    assert (
        detect_phase(
            _evidence(
                withdrawal_events=2,
                liquidity_usd=50_000.0,
                last_withdrawal_hours_ago=100.0,
            )
        )
        != LifecyclePhase.COLLAPSE
    )


def test_detect_phase_ignition_on_events_or_young_pool() -> None:
    assert detect_phase(_evidence(ignition_events=1)) == LifecyclePhase.IGNITION
    assert detect_phase(_evidence(pool_age_hours=5.0)) == LifecyclePhase.IGNITION


def test_detect_phase_parabolic_on_acceleration() -> None:
    assert (
        detect_phase(
            _evidence(
                pool_age_hours=30.0,
                volume_acceleration=3.0,
                buy_sell_ratio=1.5,
                holder_growth=10.0,
            )
        )
        == LifecyclePhase.PARABOLIC
    )


def test_detect_phase_saturation_on_sell_pressure() -> None:
    assert (
        detect_phase(_evidence(pool_age_hours=30.0, buy_sell_ratio=0.5))
        == LifecyclePhase.SATURATION
    )


def test_phase_rank_ordering() -> None:
    assert phase_rank(LifecyclePhase.SEEDING) == 0
    assert phase_rank(LifecyclePhase.COLLAPSE) == 4


def test_scan_emits_idempotent_transitions(session) -> None:
    asset = seed_market_asset(session)
    decision_ts = T0
    engine = LifecycleEngine()

    first = engine.scan(session, decision_ts=decision_ts)
    second = engine.scan(session, decision_ts=decision_ts)

    assert first["events"] == 1
    assert second["events"] == 0
    count = session.scalar(select(func.count()).select_from(models.LifecycleEvent))
    assert count == 1
    event = session.scalar(select(models.LifecycleEvent))
    assert event.asset_id == asset.id
    assert event.event_type == "phase_transition"
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "lifecycle")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert health.state == "ok"


def test_scan_advances_monotonically_to_collapse(session) -> None:
    asset = seed_market_asset(session)
    engine = LifecycleEngine()
    engine.scan(session, decision_ts=T0)  # ignition
    # Liquidity withdrawal event => collapse phase.
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    session.add(
        models.IgnitionEvent(
            asset_id=asset.id,
            source_id=source.id,
            event_type=IgnitionEventType.LIQUIDITY_WITHDRAWAL.value,
            ts=T0 + timedelta(hours=1),
            observed_at=T0 + timedelta(hours=1),
            confidence=0.9,
            details={"drop_pct": 60.0},
        )
    )
    session.commit()
    result = engine.scan(session, decision_ts=T0 + timedelta(hours=2))
    assert result["events"] == 1
    latest = session.scalar(
        select(models.LifecycleEvent)
        .where(models.LifecycleEvent.asset_id == asset.id)
        .order_by(models.LifecycleEvent.ts.desc())
        .limit(1)
    )
    assert latest.phase == LifecyclePhase.COLLAPSE.value


def test_scan_emits_terminal_phase_alert_once(session) -> None:
    asset = seed_market_asset(session)
    engine = LifecycleEngine()

    # IGNITION (young pool) fires an entry alert since Task 11 — entry
    # transitions alert with the same one-shot treatment as danger phases.
    engine.scan(session, decision_ts=T0)
    session.commit()
    alerts = session.scalars(
        select(models.Alert).where(models.Alert.alert_type == "lifecycle_transition")
    ).all()
    assert len(alerts) == 1
    assert "IGNITION" in alerts[0].message

    # Liquidity withdrawal => COLLAPSE => the operator gets an alert.
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    session.add(
        models.IgnitionEvent(
            asset_id=asset.id,
            source_id=source.id,
            event_type=IgnitionEventType.LIQUIDITY_WITHDRAWAL.value,
            ts=T0 + timedelta(hours=1),
            observed_at=T0 + timedelta(hours=1),
            confidence=0.9,
            details={"drop_pct": 60.0},
        )
    )
    session.commit()
    result = engine.scan(session, decision_ts=T0 + timedelta(hours=2))
    session.commit()
    assert result["events"] == 1
    alert = session.scalar(
        select(models.Alert).where(
            models.Alert.alert_type == "lifecycle_transition",
            models.Alert.asset_id == asset.id,
            models.Alert.message.contains("COLLAPSE"),
        )
    )
    assert alert is not None
    assert alert.state == "open"
    assert "COLLAPSE" in alert.message
    assert alert.score_snapshot_ref.startswith("lifecycle:")

    # Re-scan must not duplicate either alert (idempotent on the event ref).
    engine.scan(session, decision_ts=T0 + timedelta(hours=3))
    session.commit()
    count = session.scalar(
        select(func.count())
        .select_from(models.Alert)
        .where(models.Alert.alert_type == "lifecycle_transition")
    )
    assert count == 2


def _lifecycle_alerts(session, asset_id: int) -> list:
    return session.scalars(
        select(models.Alert).where(
            models.Alert.alert_type == "lifecycle_transition",
            models.Alert.asset_id == asset_id,
        )
    ).all()


def test_ignition_transition_fires_one_alert(session) -> None:
    """Entry transitions alert like danger phases (Task 11): a
    SEEDING -> IGNITION transition fires exactly one deduped
    lifecycle_transition alert for the event."""
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Seed1111111111111111111111111111111111111",
        symbol="SEED",
        name="Seed Fixture",
        first_seen_at=T0 - timedelta(hours=1),
    )
    session.add(
        models.SocialMention(
            asset_id=asset.id,
            source_id=source.id,
            ts=T0 - timedelta(minutes=30),
            observed_at=T0 - timedelta(minutes=30),
        )
    )
    session.commit()
    engine = LifecycleEngine()

    # SEEDING (social seed, no pool yet): a phase event, but no alert.
    result = engine.scan(session, decision_ts=T0)
    session.commit()
    assert result["events"] == 1
    seeding = session.scalar(
        select(models.LifecycleEvent).where(models.LifecycleEvent.asset_id == asset.id)
    )
    assert seeding.phase == LifecyclePhase.SEEDING.value
    assert _lifecycle_alerts(session, asset.id) == []

    # The pool appears => IGNITION => the operator gets one alert.
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="USDC111111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=T0 - timedelta(days=365),
    )
    upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address="PairSeed111111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0,
    )
    session.commit()
    result = engine.scan(session, decision_ts=T0 + timedelta(hours=1))
    session.commit()
    assert result["events"] == 1
    alerts = _lifecycle_alerts(session, asset.id)
    assert len(alerts) == 1
    assert alerts[0].state == "open"
    assert "IGNITION" in alerts[0].message
    assert alerts[0].score_snapshot_ref.startswith("lifecycle:")

    # Re-scan must not duplicate the alert (idempotent on the event ref).
    engine.scan(session, decision_ts=T0 + timedelta(hours=2))
    session.commit()
    assert len(_lifecycle_alerts(session, asset.id)) == 1


def test_parabolic_transition_fires_one_alert(session) -> None:
    asset = seed_market_asset(session)
    engine = LifecycleEngine()

    # IGNITION (young pool) fires its own entry alert under the new rule.
    engine.scan(session, decision_ts=T0)
    session.commit()
    ignition_alerts = [a for a in _lifecycle_alerts(session, asset.id) if "IGNITION" in a.message]
    assert len(ignition_alerts) == 1

    # Volume detonates a day later => PARABOLIC => one more alert.
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=T0 + timedelta(hours=29),
        observed_at=T0 + timedelta(hours=29),
        price_usd=1.10,
        volume_usd=1_000_000,
        buys=200,
        sells=5,
        trades=205,
    )
    session.commit()
    result = engine.scan(session, decision_ts=T0 + timedelta(hours=30))
    session.commit()
    assert result["events"] == 1
    parabolic_alerts = [a for a in _lifecycle_alerts(session, asset.id) if "PARABOLIC" in a.message]
    assert len(parabolic_alerts) == 1

    # Re-scan must not duplicate either entry alert.
    engine.scan(session, decision_ts=T0 + timedelta(hours=31))
    session.commit()
    assert len(_lifecycle_alerts(session, asset.id)) == 2


def test_scan_skips_assets_without_evidence(session) -> None:
    chain = get_or_create_chain(
        session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
    )
    source = get_or_create_source(
        session,
        name="dexscreener",
        source_type="market_data",
        tier="venue",
        base_url="https://api.dexscreener.com",
    )
    upsert_asset(
        session,
        chain_id=chain.id,
        address="Lonely11111111111111111111111111111111111",
        symbol="LONE",
        name="Lonely",
        first_seen_at=T0,
    )
    store_raw_evidence(session, source=source, payload={"lone": True}, observed_at=T0)
    session.commit()
    result = LifecycleEngine().scan(session, decision_ts=T0)
    assert result["events"] == 0
    assert session.scalar(select(func.count()).select_from(models.LifecycleEvent)) == 0


def test_market_rows_feed_volume_and_ratio(session) -> None:
    asset = seed_market_asset(session)
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    for idx, (volume, buys, sells) in enumerate([(1_000, 10, 5), (10_000, 50, 5), (50_000, 80, 4)]):
        ts = T0 + timedelta(minutes=30 * idx)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=1.0 + idx * 0.1,
            volume_usd=volume,
            buys=buys,
            sells=sells,
            trades=buys + sells,
        )
    session.commit()
    evidence = LifecycleEngine()._evidence(session, asset, T0 + timedelta(hours=1))
    assert evidence is not None
    assert evidence.volume_acceleration is not None and evidence.volume_acceleration > 2.0
    assert evidence.buy_sell_ratio is not None and evidence.buy_sell_ratio > 3.0
