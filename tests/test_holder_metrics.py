from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from features.factory import FeatureFactory
from ingestion.holder_tracker import record_dev_wallet_stats, record_holder_metrics
from storage import models
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


def test_non_aligned_observations_use_actual_elapsed_hours(session) -> None:
    """Snapshots at 10:15 and 11:45 keep their real observation times, so
    growth uses the actual 1.5-hour delta rather than floored hour buckets."""
    asset = seed_market_asset(session)
    first = record_holder_metrics(
        session,
        asset.id,
        top10_pct=None,
        holder_count=100,
        observed_at=datetime(2026, 5, 1, 10, 15, tzinfo=UTC),
    )
    second = record_holder_metrics(
        session,
        asset.id,
        top10_pct=None,
        holder_count=300,
        observed_at=datetime(2026, 5, 1, 11, 45, tzinfo=UTC),
    )
    assert first.ts == datetime(2026, 5, 1, 10, 15, tzinfo=UTC)
    assert second.ts == datetime(2026, 5, 1, 11, 45, tzinfo=UTC)
    assert second.holder_growth_per_hour == Decimal("133.333333")


def test_exact_holder_count_and_hourly_growth_use_metrics(session) -> None:
    """The exact metric count and hourly growth survive even though only ten
    Holder rows exist, while concentration still comes from those rows."""
    asset = seed_market_asset(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None
    for index in range(10):
        session.add(
            models.Holder(
                asset_id=asset.id,
                wallet_address=f"holder-{index}",
                source_id=source.id,
                ts=NOW,
                observed_at=NOW,
                balance=1.0,
                pct_supply=0.01,
            )
        )
    record_holder_metrics(
        session,
        asset.id,
        top10_pct=None,
        holder_count=100,
        observed_at=NOW - timedelta(hours=2),
    )
    # The metric's top10 value is deliberately different: the feature must use
    # the holder list for concentration, not the persisted aggregate.
    record_holder_metrics(
        session,
        asset.id,
        top10_pct=0.9,
        holder_count=250,
        observed_at=NOW,
    )
    session.flush()

    values = {value.name: value for value in FeatureFactory().build_for_asset(session, asset, NOW)}

    assert values["holder_count"].value == 250.0
    assert values["holder_growth"].value == pytest.approx(75.0)
    assert values["top_holder_concentration"].value == pytest.approx(0.10)


def test_metric_only_holder_count_without_holder_rows_is_safe(session) -> None:
    """A GoPlus exact count can exist before any ranked Holder rows land; the
    feature pass must not call ``ensure_utc`` on a missing holder timestamp."""
    asset = seed_market_asset(session)
    record_holder_metrics(
        session,
        asset.id,
        top10_pct=None,
        holder_count=42,
        observed_at=NOW,
    )

    values = {value.name: value for value in FeatureFactory().build_for_asset(session, asset, NOW)}

    assert values["holder_count"].value == 42.0
    assert values["holder_growth"].missing is True
    assert values["top_holder_concentration"].missing is True


def test_dev_wallet_stats_unknown_stays_none(session) -> None:
    asset = seed_market_asset(session)
    row = record_dev_wallet_stats(session, asset.id, observed_at=NOW)
    assert row.deployer is None
    assert row.dev_holding_pct is None
    assert row.dev_sold is None
