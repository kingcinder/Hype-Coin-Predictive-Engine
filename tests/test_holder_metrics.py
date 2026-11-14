from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ingestion.holder_tracker import record_dev_wallet_stats, record_holder_metrics
from tests.conftest import seed_market_asset

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_no_earlier_row_growth_is_none(session) -> None:
    asset = seed_market_asset(session)
    row = record_holder_metrics(
        session, asset.id, top10_pct=None, holder_count=100, observed_at=NOW
    )
    assert row.holder_growth_per_hour is None
    assert row.top10_pct is None
    assert row.holder_count == 100


def test_two_hours_growth_is_hundred(session) -> None:
    asset = seed_market_asset(session)
    record_holder_metrics(session, asset.id, top10_pct=0.5, holder_count=100, observed_at=NOW)
    row = record_holder_metrics(
        session,
        asset.id,
        top10_pct=None,
        holder_count=300,
        observed_at=NOW + timedelta(hours=2),
    )
    assert row.holder_growth_per_hour == Decimal("100")


def test_dev_wallet_stats_unknown_stays_none(session) -> None:
    asset = seed_market_asset(session)
    row = record_dev_wallet_stats(session, asset.id, observed_at=NOW)
    assert row.deployer is None
    assert row.dev_holding_pct is None
    assert row.dev_sold is None
