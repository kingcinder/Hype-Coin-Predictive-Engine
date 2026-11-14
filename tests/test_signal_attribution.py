from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from api.trading_routes import signal_attribution
from radar.ignition import IgnitionRadar
from storage import models
from tests.conftest import seed_market_asset, seed_reference

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _trade(session, index: int, *, source: str, pnl: Decimal) -> None:
    session.add(
        models.LedgerTrade(
            mode="paper",
            opened_at=NOW,
            closed_at=NOW + timedelta(minutes=index),
            mint=f"M{index}",
            chain="solana",
            source_signal=source,
            entry_price=Decimal("1"),
            size_sol=Decimal("1"),
            fees_sol=Decimal("0"),
            slippage_sol=Decimal("0"),
            exit_price=Decimal("1"),
            pnl_sol=pnl,
            why_entered="test",
            why_exited="test",
            lesson="test",
            candidate_evidence_ts=NOW,
            price_source="fake",
            price_source_ts=NOW,
            rules_sha256="abc",
            created_at=NOW,
        )
    )


def test_attribution_groups_and_insufficient_data(session) -> None:
    for index in range(12):
        _trade(
            session,
            index,
            source="ignition:1:1",
            pnl=Decimal("1") if index < 7 else Decimal("-1"),
        )
    for index in range(4):
        _trade(session, 20 + index, source="other:2:2", pnl=Decimal("1"))
    session.commit()

    groups = asyncio.run(signal_attribution(session))
    ignition = next(group for group in groups if group["signal_type"] == "ignition")
    assert ignition["trades"] == 12
    assert ignition["wins"] == 7
    assert "mean_pnl_sol" in ignition
    other = next(group for group in groups if group["signal_type"] == "other")
    assert other["trades"] == 4
    assert other["insufficient_data"] is True


def test_sniper_burst_event_carries_signal_id(session) -> None:
    asset = seed_market_asset(session)
    _chain, source = seed_reference(session)
    event_ts = NOW - timedelta(minutes=5)
    IgnitionRadar().upsert_event(
        session,
        asset=asset,
        source_id=source.id,
        event_type="sniper_burst",
        ts=event_ts,
        observed_at=NOW,
        confidence=0.8,
        details={},
    )
    session.commit()
    event = session.query(models.IgnitionEvent).one()
    assert event.details["signal_id"] == f"ignition:{asset.id}:{int(event_ts.timestamp())}"
    assert event.details["event_ts"] == event_ts.isoformat()
