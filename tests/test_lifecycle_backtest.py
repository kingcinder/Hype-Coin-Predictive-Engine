from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from common.enums import IgnitionEventType
from pump_physics.backtest import (
    IGNITION_PHASES,
    LifecycleBacktestConfig,
    LifecycleBacktestRunner,
)
from storage import models
from storage.repository import (
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
    upsert_social_mention,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _seed_arc(
    session,
    *,
    symbol: str,
    prices: list[float],
    volume: float = 1_000.0,
    buys: int = 10,
    sells: int = 10,
) -> models.Asset:
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"AddrLC{symbol}1111111111111111111111111111111111",
        symbol=symbol,
        name=symbol,
        first_seen_at=T0,
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"QuoteLC{symbol}111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=T0 - timedelta(days=365),
    )
    _, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=f"PairLC{symbol}111111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0,
    )
    for hour, price in enumerate(prices):
        ts = T0 + timedelta(hours=hour)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=volume,
            buys=buys,
            sells=sells,
            trades=buys + sells,
        )
    return asset


def _metrics(session, run: models.BacktestRun) -> dict[str, float]:
    return {
        row.metric_name: row.metric_value
        for row in session.scalars(
            select(models.BacktestResult).where(models.BacktestResult.run_id == run.id)
        )
    }


def _run(session, *, start: datetime, end: datetime) -> models.BacktestRun:
    runner = LifecycleBacktestRunner()
    return runner.run(
        session,
        LifecycleBacktestConfig(start=start, end=end, step_hours=1, forward_hours=24),
    )


def test_walk_forward_measures_transition_predictions(session) -> None:
    # CRASH: pumps +100% at hour 3, falls -25%/h at hour 5, trough -80% at hour 6.
    crash = _seed_arc(
        session,
        symbol="CRASH",
        prices=[1.0, 1.0, 1.0, 2.0, 1.6, 1.2, 0.35] + [0.35] * 18,
    )
    # COOL: flat forever -> its ignition transition is a false alarm.
    cool = _seed_arc(session, symbol="COOL", prices=[1.0] * 25)
    # PUMP: +100% at hour 2 -> ignition predicts the pump.
    _seed_arc(session, symbol="PUMP", prices=[1.0, 1.0, 2.0] + [2.0] * 22)
    session.commit()

    run = _run(session, start=T0, end=T0 + timedelta(hours=7))
    session.commit()
    assert run.status == "completed"
    assert run.model_version == "lifecycle-rules-v1"
    metrics = _metrics(session, run)

    # IGNITION: three transitions (one per asset), two true pumps, one false alarm.
    assert metrics["lifecycle.ignition.transitions"] == 3.0
    assert metrics["lifecycle.ignition.true_positives"] == 2.0
    assert metrics["lifecycle.ignition.false_alarms"] == 1.0
    assert metrics["lifecycle.ignition.precision"] == pytest.approx(2.0 / 3.0, abs=1e-4)
    assert metrics["lifecycle.ignition.false_alarm_rate"] == pytest.approx(1.0 / 3.0, abs=1e-4)
    # Leads: CRASH 180min (hour 3), PUMP 120min (hour 2) -> median 150.
    assert metrics["lifecycle.ignition.median_lead_minutes"] == 150.0

    # COLLAPSE: CRASH's -25%/h hour-5 transition predicts the -70% trough at hour 6.
    assert metrics["lifecycle.collapse.transitions"] == 1.0
    assert metrics["lifecycle.collapse.true_positives"] == 1.0
    assert metrics["lifecycle.collapse.false_alarms"] == 0.0
    assert metrics["lifecycle.collapse.precision"] == 1.0
    assert metrics["lifecycle.collapse.median_lead_minutes"] == 60.0

    # Overall: (2+1) TP / (3+1) transitions.
    assert metrics["lifecycle.overall.precision"] == pytest.approx(0.75)
    assert metrics["lifecycle.overall.false_alarm_rate"] == pytest.approx(0.25)
    assert metrics["lifecycle.overall.median_ignition_lead_minutes"] == 150.0
    assert metrics["lifecycle.overall.median_collapse_lead_minutes"] == 60.0
    assert metrics["lifecycle.decision_steps"] == 8.0
    assert metrics["lifecycle.assets_with_transitions"] == 3.0
    assert metrics["lifecycle.unevaluated_transitions"] == 0.0
    assert crash.id is not None and cool.id is not None


def test_zero_transitions_report_zero_metrics(session) -> None:
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="NoPool111111111111111111111111111111111111",
        symbol="SEED",
        name="Seed-only",
        first_seen_at=T0,
    )
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="SEED shill",
        source_id=source.id,
        ts=T0,
        observed_at=T0,
        metrics_json={"channel": "@seed"},
        raw_ref="t.me/seed/1",
    )
    session.commit()

    run = _run(session, start=T0, end=T0 + timedelta(hours=4))
    session.commit()
    metrics = _metrics(session, run)
    assert metrics["lifecycle.ignition.transitions"] == 0.0
    assert metrics["lifecycle.collapse.transitions"] == 0.0
    assert metrics["lifecycle.ignition.precision"] == 0.0
    assert metrics["lifecycle.collapse.precision"] == 0.0
    assert metrics["lifecycle.overall.precision"] == 0.0
    assert metrics["lifecycle.assets_with_transitions"] == 0.0


def test_walk_forward_fires_only_when_evidence_is_visible(session) -> None:
    """A late-arriving withdrawal event must not fire COLLAPSE early."""
    _seed_arc(session, symbol="SNEAK", prices=[1.0] * 7)
    session.flush()
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    asset = session.scalar(select(models.Asset).where(models.Asset.symbol == "SNEAK"))
    session.add(
        models.IgnitionEvent(
            asset_id=asset.id,
            source_id=source.id,
            event_type=IgnitionEventType.LIQUIDITY_WITHDRAWAL.value,
            # The withdrawal *happened* at T0+1h but was only *observed* at T0+4h.
            ts=T0 + timedelta(hours=1),
            observed_at=T0 + timedelta(hours=4),
            confidence=0.9,
            details={"drop_pct": 60.0},
        )
    )
    session.commit()

    run = _run(session, start=T0, end=T0 + timedelta(hours=6))
    session.commit()
    metrics = _metrics(session, run)
    # IGNITION fires at T0 (young pool); COLLAPSE fires exactly once, only after
    # the withdrawal becomes visible at T0+4h — never from its ts.
    assert metrics["lifecycle.ignition.transitions"] == 1.0
    assert metrics["lifecycle.collapse.transitions"] == 1.0
    assert metrics["lifecycle.decision_steps"] == 7.0


def test_measured_phase_sets_are_disjoint() -> None:
    from common.enums import LifecyclePhase
    from pump_physics.backtest import COLLAPSE_PHASES

    assert IGNITION_PHASES.isdisjoint(COLLAPSE_PHASES)
    assert IGNITION_PHASES == {LifecyclePhase.IGNITION, LifecyclePhase.PARABOLIC}
