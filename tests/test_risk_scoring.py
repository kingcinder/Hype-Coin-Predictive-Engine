from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from features.factory import FeatureFactory
from ingestion.rpc_pool import RpcEndpointPool
from risk_engine.rules import assess_risk
from scoring.engine import ScoringEngine, score_current_assets
from scoring.formulas import compute_scores
from storage import models
from storage.repository import insert_liquidity_snapshot_once, insert_market_snapshot_once
from tests.conftest import seed_market_asset


def test_low_liquidity_black_hard_reject_overrides_hype() -> None:
    features = {
        "five_min_return": 80.0,
        "one_hour_return": 150.0,
        "volume_acceleration": 10.0,
        "liquidity_depth": 1000.0,
        "liquidity_change": 25.0,
        "buy_sell_ratio": 4.0,
        "unique_buyers_estimate": 100.0,
        "pair_age_minutes": 5.0,
        "holder_count": 20.0,
        "holder_growth": 10.0,
        "top_holder_concentration": 0.1,
        "spread_estimate": 5.0,
        "volatility": 10.0,
        "venue_agreement": 100.0,
        "mention_velocity": 50.0,
        "website_presence": 1.0,
        "github_presence_public": 1.0,
        "suspicious_contract_flags": 0.0,
        "deployer_history_available": 0.0,
        "narrative_acceleration": 3.0,
    }
    result = compute_scores(features, [])
    assert result.risk_band.value == "BLACK"
    assert result.research_priority == 0.0


def test_rpc_data_layer_degradation_widens_uncertainty() -> None:
    healthy = compute_scores({}, [])
    degraded = compute_scores({}, [], data_layer_uncertainty=35.0)
    assert degraded.confidence < healthy.confidence
    assert degraded.uncertainty > healthy.uncertainty
    assert degraded.drivers["rpc_pool_uncertainty"] == 35.0


def test_rpc_pool_health_is_a_persisted_feature_and_score_driver(session, monkeypatch) -> None:
    asset = seed_market_asset(session)
    pool = RpcEndpointPool(
        ["https://rpc-healthy.example.com", "https://rpc-down.example.com"],
        failure_threshold=1,
        chain_slug="solana",
    )
    pool.mark_failure("https://rpc-down.example.com")
    monkeypatch.setattr("features.factory.get_rpc_pool", lambda _chain: pool)
    values = {
        value.name: value
        for value in FeatureFactory().build_for_asset(session, asset, datetime.now(UTC))
    }
    assert values["rpc_pool_health"].value == 0.5
    assert values["rpc_pool_health"].missing is False

    result = compute_scores({"rpc_pool_health": values["rpc_pool_health"].value}, [])
    assert result.drivers["rpc_pool_health"] == 50.0


def test_scoring_engine_reads_the_asset_chain_rpc_pool(session, monkeypatch) -> None:
    asset = seed_market_asset(session)
    pool = RpcEndpointPool(
        ["https://rpc-healthy.example.com", "https://rpc-down.example.com"],
        failure_threshold=1,
        chain_slug="solana",
    )
    pool.mark_failure("https://rpc-down.example.com")
    monkeypatch.setattr("scoring.engine.get_rpc_pool", lambda _chain: pool)
    assert ScoringEngine()._rpc_pool_uncertainty(session, asset.id) == 50.0


def test_extreme_holder_concentration_is_black() -> None:
    assessment = assess_risk(
        {
            "liquidity_depth": 200_000.0,
            "top_holder_concentration": 0.95,
            "pair_age_minutes": 120.0,
            "suspicious_contract_flags": 0.0,
        }
    )
    assert assessment.band.value == "BLACK"
    assert assessment.hard_reject is True


def test_liquidity_withdrawal_signal_raises_risk() -> None:
    assessment = assess_risk(
        {
            "liquidity_depth": 200_000.0,
            "top_holder_concentration": 0.1,
            "pair_age_minutes": 120.0,
            "suspicious_contract_flags": 0.0,
            "liquidity_withdrawal_signal": 1.0,
        }
    )
    assert assessment.band.value == "YELLOW"
    assert any("withdrawal" in reason for reason in assessment.reasons)


def test_withdrawal_on_shallow_book_is_hard_reject() -> None:
    assessment = assess_risk(
        {
            "liquidity_depth": 30_000.0,
            "top_holder_concentration": 0.1,
            "pair_age_minutes": 120.0,
            "suspicious_contract_flags": 0.0,
            "liquidity_withdrawal_signal": 1.0,
        }
    )
    assert assessment.hard_reject is True
    assert assessment.band.value == "BLACK"


def test_recidivism_overlap_raises_risk() -> None:
    assessment = assess_risk(
        {
            "liquidity_depth": 200_000.0,
            "top_holder_concentration": 0.1,
            "pair_age_minutes": 120.0,
            "suspicious_contract_flags": 0.0,
            "recidivism_score": 70.0,
        }
    )
    assert any("recidivism" in reason for reason in assessment.reasons)


def test_score_explanation_records_changed_features(session) -> None:
    asset = seed_market_asset(session)
    first_decision = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    score_current_assets(session, decision_ts=first_decision, asset_ids=[asset.id])
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    pool = session.scalar(select(models.Pool).where(models.Pool.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert pair is not None
    assert pool is not None
    assert source is not None

    second_decision = first_decision + timedelta(hours=1)
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=second_decision,
        observed_at=second_decision,
        price_usd=1.75,
        volume_usd=95_000,
        buys=80,
        sells=8,
        trades=88,
    )
    insert_liquidity_snapshot_once(
        session,
        pool_id=pool.id,
        source_id=source.id,
        ts=second_decision,
        observed_at=second_decision,
        reserve_usd=160_000,
    )
    scores = score_current_assets(session, decision_ts=second_decision, asset_ids=[asset.id])
    explanation = session.scalar(
        select(models.ScoreExplanation).where(models.ScoreExplanation.score_id == scores[0].id)
    )
    assert explanation is not None
    assert "liquidity_depth" in explanation.changed_features
    assert explanation.changed_features["liquidity_depth"]["current"] == 160_000.0
