from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from radar.prelaunch import PrelaunchQueue
from storage import models
from storage.repository import upsert_asset, upsert_social_mention
from tests.conftest import seed_market_asset, seed_reference

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_prelaunch_ranks_without_pool_and_alerts(session) -> None:
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="TokenPre111111111111111111111111111111111",
        symbol="PRE1",
        name="Pre Token",
        first_seen_at=NOW - timedelta(days=1),
        website_url="https://example.org",
    )
    for index in range(4):
        upsert_social_mention(
            session,
            asset_id=asset.id,
            topic=f"PRE1 token presale {index}",
            source_id=source.id,
            ts=NOW - timedelta(hours=index),
            observed_at=NOW - timedelta(hours=index),
            raw_ref=f"https://reddit.com/pre1/{index}",
        )
    session.commit()

    queue = PrelaunchQueue()
    queue.settings.prelaunch_alert_threshold = 20.0
    candidates = queue.scan(session, decision_ts=NOW)
    session.commit()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.asset_id == asset.id
    assert candidate.priority_score > 0
    assert candidate.drivers["mentions_24h"] == 4

    alert = session.scalar(
        select(models.Alert).where(models.Alert.alert_type == "prelaunch_candidate")
    )
    assert alert is not None
    assert "PRE1" in alert.message

    # idempotent scan updates the same candidate row
    queue.scan(session, decision_ts=NOW)
    session.commit()
    assert (
        session.scalar(
            select(func.count())
            .select_from(models.PrelaunchCandidate)
            .where(models.PrelaunchCandidate.asset_id == asset.id)
        )
        == 1
    )


def test_launched_asset_is_not_ranked(session) -> None:
    asset = seed_market_asset(session)
    candidates = PrelaunchQueue().scan(session, decision_ts=NOW)
    session.commit()
    assert all(candidate.asset_id != asset.id for candidate in candidates)


def test_prelaunch_alert_not_duplicated_across_scans(session) -> None:
    """H16: the alert dedup key is scan-stable (asset, model version), not the
    per-scan candidate row id — a second scan with a new decision_ts must not
    fire a duplicate alert."""
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="TokenPreDedup111111111111111111111111111",
        symbol="PREDUP",
        name="Pre Dedup Token",
        first_seen_at=NOW - timedelta(days=1),
        website_url="https://example.org",
    )
    for index in range(4):
        upsert_social_mention(
            session,
            asset_id=asset.id,
            topic=f"PREDUP token presale {index}",
            source_id=source.id,
            ts=NOW - timedelta(hours=index),
            observed_at=NOW - timedelta(hours=index),
            raw_ref=f"https://reddit.com/predup/{index}",
        )
    session.commit()

    queue = PrelaunchQueue()
    queue.settings.prelaunch_alert_threshold = 20.0
    queue.scan(session, decision_ts=NOW)
    session.commit()

    # a later scan inserts a new candidate row (new decision_ts) ...
    queue.scan(session, decision_ts=NOW + timedelta(hours=1))
    session.commit()
    assert (
        session.scalar(
            select(func.count())
            .select_from(models.PrelaunchCandidate)
            .where(models.PrelaunchCandidate.asset_id == asset.id)
        )
        == 2
    )

    # ... but fires no duplicate alert
    alerts = session.scalars(
        select(models.Alert).where(models.Alert.alert_type == "prelaunch_candidate")
    ).all()
    assert len(alerts) == 1
    assert alerts[0].score_snapshot_ref == (
        f"prelaunch:{asset.id}:{queue.settings.prelaunch_model_version}"
    )
