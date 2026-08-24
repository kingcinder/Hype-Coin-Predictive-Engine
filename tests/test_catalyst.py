from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from catalyst.extractor import (
    alert_upcoming_catalysts,
    extract_catalyst_type,
    extract_catalysts,
    extract_scheduled_at,
)
from common.time import ensure_utc
from storage import models
from storage.repository import upsert_news_item
from tests.conftest import seed_market_asset

NOW = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)


def test_extract_catalyst_type_and_date() -> None:
    catalyst_type, confidence = extract_catalyst_type("HYPE TGE scheduled for 2026-06-01")
    assert catalyst_type == "tge"
    assert confidence > 0.5
    scheduled = extract_scheduled_at("HYPE TGE scheduled for 2026-06-01", NOW)
    assert scheduled == datetime(2026, 6, 1, tzinfo=UTC)
    assert extract_catalyst_type("just another blog post") == (None, 0.0)


def test_catalyst_extraction_and_alert_are_idempotent(session) -> None:
    seed_market_asset(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None
    upsert_news_item(
        session,
        source_id=source.id,
        published_at=NOW,
        observed_at=NOW,
        source_domain="example.com",
        title="HYPE TGE scheduled for 2026-06-01",
        url="https://example.com/hype-tge",
    )
    session.commit()

    assert extract_catalysts(session, decision_ts=NOW) == 1
    assert extract_catalysts(session, decision_ts=NOW) == 0
    session.commit()

    catalyst = session.scalar(select(models.Catalyst))
    assert catalyst is not None
    assert catalyst.catalyst_type == "tge"
    assert ensure_utc(catalyst.scheduled_at) == datetime(2026, 6, 1, tzinfo=UTC)

    assert alert_upcoming_catalysts(session, decision_ts=NOW) == 1
    assert alert_upcoming_catalysts(session, decision_ts=NOW) == 0
    session.commit()

    alert = session.scalar(
        select(models.Alert).where(models.Alert.alert_type == "upcoming_catalyst")
    )
    assert alert is not None
    assert "HYPE" in alert.message
    assert "tge" in alert.message


def test_catalyst_outside_alert_window_does_not_alert(session) -> None:
    seed_market_asset(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    upsert_news_item(
        session,
        source_id=source.id,
        published_at=NOW,
        observed_at=NOW,
        source_domain="example.com",
        title="HYPE listing scheduled for 2026-12-01",
        url="https://example.com/hype-listing",
    )
    session.commit()
    assert extract_catalysts(session, decision_ts=NOW) == 1
    assert alert_upcoming_catalysts(session, decision_ts=NOW) == 0
