from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from radar.first_buys import record_first_buys
from storage import models
from tests.conftest import seed_market_asset

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_first_window_stats_and_tracked_alert(session) -> None:
    asset = seed_market_asset(session)
    swaps = [
        {"wallet": "A", "side": "buy", "size_usd": 10, "ts": NOW + timedelta(minutes=1)},
        {"wallet": "B", "side": "buy", "size_usd": 20, "ts": NOW + timedelta(minutes=2)},
        {"wallet": "C", "side": "sell", "size_usd": 5, "ts": NOW + timedelta(minutes=3)},
    ]
    record_first_buys(session, asset.id, NOW, swaps, {"A"})
    session.commit()

    buys = session.scalars(select(models.FirstBuy)).all()
    assert len(buys) == 2
    assert next(row for row in buys if row.wallet == "A").is_tracked_wallet is True
    stats = session.scalar(
        select(models.PairFirstWindowStats).where(models.PairFirstWindowStats.asset_id == asset.id)
    )
    assert stats is not None
    assert stats.buy_sell_ratio == Decimal("2")
    alerts = session.scalars(
        select(models.Alert).where(models.Alert.alert_type == "tracked_wallet_first_buy")
    ).all()
    assert len(alerts) == 1


def test_unknown_swaps_stay_none(session) -> None:
    asset = seed_market_asset(
        session,
        address="NoSwaps111111111111111111111111111111111",
        pair_address="NoSwapsPair11111111111111111111111111111",
    )
    record_first_buys(session, asset.id, NOW, [], set())
    session.commit()
    stats = session.scalar(
        select(models.PairFirstWindowStats).where(models.PairFirstWindowStats.asset_id == asset.id)
    )
    assert stats is not None
    assert stats.buys is None
    assert stats.sells is None
    assert stats.buy_sell_ratio is None
