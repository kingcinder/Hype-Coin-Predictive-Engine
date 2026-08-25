from __future__ import annotations

from datetime import UTC, datetime, timedelta

from features.factory import FeatureFactory
from storage import models
from storage.repository import (
    get_or_create_source,
    store_raw_evidence,
    upsert_social_mention,
)
from tests.conftest import seed_market_asset


def _build_features(session, asset, decision_ts) -> dict[str, object]:
    values = FeatureFactory().build_for_asset(session, asset, decision_ts)
    return {value.name: value for value in values}


def test_velocity_features_from_crawler_metrics(session) -> None:
    asset = seed_market_asset(session)
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    youtube = get_or_create_source(
        session, name="youtube_rss", source_type="social", tier="public_metadata"
    )
    telegram = get_or_create_source(
        session, name="telegram", source_type="social", tier="public_metadata"
    )
    github = get_or_create_source(
        session, name="github_public", source_type="public_metadata", tier="public_metadata"
    )
    huggingface = get_or_create_source(
        session, name="huggingface", source_type="public_metadata", tier="public_metadata"
    )

    repo_url = "https://github.com/example/hype"
    model_url = "https://huggingface.co/example/hype"
    now_minus_1h = decision_ts - timedelta(hours=1)
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="HYPE moon video",
        source_id=youtube.id,
        ts=now_minus_1h,
        observed_at=now_minus_1h,
        metrics_json={"channel_id": "UCkol1"},
        raw_ref="https://youtube.com/watch?v=1",
    )
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="HYPE gem alert",
        source_id=telegram.id,
        ts=now_minus_1h,
        observed_at=now_minus_1h,
        metrics_json={"channel": "@hypeshills"},
        raw_ref="t.me/hypeshills/1",
    )
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="HYPE repo",
        source_id=github.id,
        ts=decision_ts,
        observed_at=decision_ts,
        metrics_json={"stars": 130},
        raw_ref=repo_url,
    )
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="HYPE model",
        source_id=huggingface.id,
        ts=decision_ts,
        observed_at=decision_ts,
        metrics_json={"downloads": 2000},
        raw_ref=model_url,
    )

    # Two crawls of the same repo 36h apart: 30 stars gained => 20 stars/day.
    store_raw_evidence(
        session,
        source=github,
        payload={"items": [{"url": repo_url, "title": "example/hype", "metrics": {"stars": 100}}]},
        observed_at=decision_ts - timedelta(hours=36),
    )
    store_raw_evidence(
        session,
        source=github,
        payload={"items": [{"url": repo_url, "title": "example/hype", "metrics": {"stars": 130}}]},
        observed_at=decision_ts,
    )
    # Two crawls of the same model 24h apart: 1000 downloads => 1000/day.
    store_raw_evidence(
        session,
        source=huggingface,
        payload={
            "items": [{"url": model_url, "title": "example/hype", "metrics": {"downloads": 1000}}]
        },
        observed_at=decision_ts - timedelta(hours=24),
    )
    store_raw_evidence(
        session,
        source=huggingface,
        payload={
            "items": [{"url": model_url, "title": "example/hype", "metrics": {"downloads": 2000}}]
        },
        observed_at=decision_ts,
    )
    session.commit()

    features = _build_features(session, asset, decision_ts)
    assert features["kol_velocity"].missing is False
    assert features["kol_velocity"].value == 2.0  # UCkol1 + @hypeshills
    assert features["github_star_velocity"].missing is False
    assert features["github_star_velocity"].value == 20.0  # 30 stars / 36h * 24
    assert features["hf_download_velocity"].missing is False
    assert features["hf_download_velocity"].value == 1000.0  # 1000 downloads / 24h * 24


def test_velocity_features_missing_without_mentions(session) -> None:
    asset = seed_market_asset(
        session, address="Token222222222222222222222222222222222222", symbol="NOISE"
    )
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    session.commit()

    features = _build_features(session, asset, decision_ts)
    assert features["kol_velocity"].missing is True
    assert features["kol_velocity"].value == 0.0
    assert features["github_star_velocity"].missing is True
    assert features["hf_download_velocity"].missing is True


def test_kol_velocity_respects_24h_window(session) -> None:
    asset = seed_market_asset(session)
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    youtube = get_or_create_source(
        session, name="youtube_rss", source_type="social", tier="public_metadata"
    )
    # A KOL mention older than 24h must not count toward the trailing window.
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="HYPE old video",
        source_id=youtube.id,
        ts=decision_ts - timedelta(hours=48),
        observed_at=decision_ts - timedelta(hours=48),
        metrics_json={"channel_id": "UCstale"},
        raw_ref="https://youtube.com/watch?v=stale",
    )
    session.commit()

    features = _build_features(session, asset, decision_ts)
    assert features["kol_velocity"].missing is True
    assert features["kol_velocity"].value == 0.0


def test_star_velocity_requires_two_observations(session) -> None:
    asset = seed_market_asset(session)
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    github = get_or_create_source(
        session, name="github_public", source_type="public_metadata", tier="public_metadata"
    )
    repo_url = "https://github.com/example/hype"
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="HYPE repo",
        source_id=github.id,
        ts=decision_ts,
        observed_at=decision_ts,
        metrics_json={"stars": 130},
        raw_ref=repo_url,
    )
    store_raw_evidence(
        session,
        source=github,
        payload={"items": [{"url": repo_url, "metrics": {"stars": 130}}]},
        observed_at=decision_ts,
    )
    session.commit()

    features = _build_features(session, asset, decision_ts)
    assert features["github_star_velocity"].missing is True
    assert features["github_star_velocity"].value == 0.0


def test_velocity_features_persist_via_score_scan(session) -> None:
    from sqlalchemy import select

    from scoring.engine import score_current_assets

    asset = seed_market_asset(session)
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    youtube = get_or_create_source(
        session, name="youtube_rss", source_type="social", tier="public_metadata"
    )
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="HYPE video",
        source_id=youtube.id,
        ts=decision_ts - timedelta(hours=1),
        observed_at=decision_ts - timedelta(hours=1),
        metrics_json={"channel_id": "UCkol1"},
        raw_ref="https://youtube.com/watch?v=1",
    )
    session.commit()

    score_current_assets(session, decision_ts=decision_ts, asset_ids=[asset.id])
    feature = session.scalar(
        select(models.Feature).where(
            models.Feature.asset_id == asset.id,
            models.Feature.decision_ts == decision_ts,
            models.Feature.feature_name == "kol_velocity",
        )
    )
    assert feature is not None
    assert feature.feature_value == 1.0
    assert feature.missing_flag is False
