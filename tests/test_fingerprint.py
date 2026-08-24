from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from fingerprint.engine import FingerprintEngine
from scoring.engine import score_current_assets
from storage import models
from storage.repository import upsert_contract
from tests.conftest import seed_market_asset

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _add_launch_wallets(
    session, asset: models.Asset, wallets: list[str], *, deployer: str | None = None
) -> None:
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None
    if deployer:
        contract = session.scalar(
            select(models.Contract).where(models.Contract.asset_id == asset.id)
        )
        if contract:
            contract.deployer_wallet = deployer
        else:
            upsert_contract(
                session,
                chain_id=asset.chain_id,
                asset_id=asset.id,
                address=asset.address,
                observed_at=NOW - timedelta(hours=1),
                deployer_wallet=deployer,
            )
    for wallet in wallets:
        session.add(
            models.Holder(
                asset_id=asset.id,
                wallet_address=wallet,
                source_id=source.id,
                ts=NOW - timedelta(hours=1),
                observed_at=NOW - timedelta(hours=1),
                balance=1_000_000,
                pct_supply=0.1,
            )
        )


def test_learn_and_recidivism_assessment(session) -> None:
    toxic_assets = [
        seed_market_asset(
            session,
            low_liquidity=True,
            address=f"Toxic{idx}111111111111111111111111111111111111",
            symbol=f"TOX{idx}",
            pair_address=f"PairToxic{idx}111111111111111111111111111111",
        )
        for idx in range(3)
    ]
    for asset in toxic_assets:
        _add_launch_wallets(
            session,
            asset,
            wallets=["wallet-a", "wallet-b", f"wallet-extra-{asset.id}"],
            deployer="wallet-a",
        )
    score_current_assets(session, decision_ts=NOW, asset_ids=[asset.id for asset in toxic_assets])

    fresh = seed_market_asset(
        session,
        address="Fresh1111111111111111111111111111111111111",
        symbol="FRESH",
        pair_address="PairFresh1111111111111111111111111111111",
    )
    _add_launch_wallets(session, fresh, wallets=["wallet-a", "wallet-b"], deployer="wallet-a")
    clean = seed_market_asset(
        session,
        address="Clean1111111111111111111111111111111111111",
        symbol="CLEAN",
        pair_address="PairClean1111111111111111111111111111111",
    )
    _add_launch_wallets(session, clean, wallets=["wallet-x", "wallet-y"])
    session.commit()

    engine = FingerprintEngine()
    engine.settings.recidivism_alert_threshold = 40.0
    decision = NOW + timedelta(minutes=30)
    assert engine.learn(session, decision_ts=decision) == 1
    # cluster learning is idempotent
    assert engine.learn(session, decision_ts=decision) == 0
    assert session.scalar(select(func.count()).select_from(models.WalletCluster)) == 1

    assessments = engine.assess(
        session, decision_ts=decision, asset_ids=[fresh.id, clean.id]
    )
    session.commit()
    assert len(assessments) == 2
    by_asset = {assessment.asset_id: assessment for assessment in assessments}

    fresh_assessment = by_asset[fresh.id]
    assert fresh_assessment.matched_cluster_count == 1
    assert fresh_assessment.matched_wallet_count == 2
    assert "deployer" in fresh_assessment.matched_roles
    assert fresh_assessment.recidivism_score >= 40.0
    assert fresh_assessment.matched_clusters[0]["toxic_rate"] == 1.0

    assert by_asset[clean.id].recidivism_score == 0.0
    assert by_asset[clean.id].matched_cluster_count == 0

    alert = session.scalar(
        select(models.Alert).where(
            models.Alert.asset_id == fresh.id,
            models.Alert.alert_type == "syndicate_recidivism",
        )
    )
    assert alert is not None
    assert "FRESH" in alert.message

    # assessment upsert is idempotent
    again = engine.assess(session, decision_ts=decision, asset_ids=[fresh.id, clean.id])
    session.commit()
    assert len(again) == 2
    assert session.scalar(select(func.count()).select_from(models.FingerprintAssessment)) == 2


def test_clean_asset_without_overlap_gets_zero_recidivism(session) -> None:
    asset = seed_market_asset(session)
    _add_launch_wallets(session, asset, wallets=["wallet-p", "wallet-q"])
    session.commit()
    engine = FingerprintEngine()
    assessments = engine.assess(session, decision_ts=NOW, asset_ids=[asset.id])
    session.commit()
    assert len(assessments) == 1
    assert assessments[0].recidivism_score == 0.0
    assert assessments[0].matched_cluster_count == 0


def test_fingerprint_health_recorded(session) -> None:
    seed_market_asset(session)
    engine = FingerprintEngine()
    engine.learn(session, decision_ts=NOW)
    engine.assess(session, decision_ts=NOW)
    session.commit()
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "fingerprint")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert health is not None
    assert health.state == "ok"
