from __future__ import annotations

from datetime import UTC, datetime

from tests.conftest import seed_market_asset


def test_whale_telemetry_nullable_columns_round_trip(session) -> None:
    from storage import models

    asset = seed_market_asset(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    session.add_all(
        [
            models.FirstBuy(
                asset_id=asset.id,
                wallet="W",
                size_usd=None,
                seconds_after_pair_creation=None,
                is_tracked_wallet=False,
                ts=now,
            ),
            models.PairFirstWindowStats(
                asset_id=asset.id,
                window_minutes=None,
                buys=None,
                sells=None,
                buy_sell_ratio=None,
                computed_at=None,
            ),
            models.DevWalletStats(
                asset_id=asset.id,
                deployer=None,
                dev_holding_pct=None,
                dev_sold=None,
                last_checked_ts=None,
            ),
            models.HolderMetric(
                asset_id=asset.id,
                ts=now,
                top10_pct=None,
                holder_count=None,
                holder_growth_per_hour=None,
            ),
        ]
    )
    session.commit()
    assert session.query(models.FirstBuy).one().size_usd is None
    assert session.query(models.PairFirstWindowStats).one().buys is None
    assert session.query(models.DevWalletStats).one().deployer is None
    assert session.query(models.HolderMetric).one().holder_count is None
