from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from features.factory import FeatureFactory
from radar.liquidity import BURN_TOPIC, TRANSFER_TOPIC, ZERO_TOPIC, LiquidityRemovalWatcher
from risk_engine.rules import assess_risk
from storage import models
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    upsert_asset,
    upsert_pool_and_pair,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _seed_base_pool(session):
    chain = get_or_create_chain(
        session, "base", name="Base", vm_type="evm", native_symbol="ETH"
    )
    source = get_or_create_source(
        session,
        name="evm_rpc",
        source_type="chain_rpc",
        tier="chain",
        base_url="https://base.example.com",
    )
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="0xAsset000000000000000000000000000000000001",
        symbol="LPX",
        name="LP Exit",
        first_seen_at=NOW,
    )
    pool, _ = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="uniswap-v2",
        pair_address="0xPool000000000000000000000000000000000001",
        base_asset_id=asset.id,
        quote_asset_id=None,
        created_at_source=NOW,
    )
    session.commit()
    return asset, pool, source


def test_liquidity_watcher_detects_burns_and_withdrawals_before_risk(session, monkeypatch) -> None:
    asset, pool, _ = _seed_base_pool(session)
    logs = [
        {
            "address": pool.address,
            "transactionHash": "0xtx-burn",
            "blockNumber": "0x64",
            "logIndex": "0x1",
            "topics": [TRANSFER_TOPIC, "0x" + "1" * 64, ZERO_TOPIC],
            "data": "0x",
        },
        {
            "address": pool.address,
            "transactionHash": "0xtx-withdraw",
            "blockNumber": "0x65",
            "logIndex": "0x2",
            "topics": [BURN_TOPIC],
            "data": "0x" + "0" * 128,
        },
    ]
    watcher = LiquidityRemovalWatcher()
    monkeypatch.setattr(watcher, "_logs_for_pools", lambda _chain, _addresses: logs)

    result = watcher.scan(session, decision_ts=NOW)
    session.commit()
    assert result["events"] == 2
    assert result["lp_burns"] == 1
    assert result["withdrawals"] == 1
    rows = session.scalars(select(models.LiquidityRemovalEvent)).all()
    assert {row.event_kind for row in rows} == {"lp_burn", "liquidity_withdrawal"}
    assert {row.tx_hash for row in rows} == {"0xtx-burn", "0xtx-withdraw"}

    # The same RPC log batch is idempotent.
    assert watcher.scan(session, decision_ts=NOW)["events"] == 0
    session.commit()

    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, NOW)
    }
    assert values["lp_removal_signal"].value == 2.0
    assert values["lp_removal_signal"].missing is False

    assessment = assess_risk(
        {
            "liquidity_depth": 200_000.0,
            "pair_age_minutes": 120.0,
            "lp_removal_signal": values["lp_removal_signal"].value,
        }
    )
    assert assessment.score > 0
    assert any("LP burn/withdrawal" in reason for reason in assessment.reasons)


def test_transfer_mint_is_not_mistaken_for_lp_burn(session) -> None:
    asset, pool, _ = _seed_base_pool(session)
    parsed = LiquidityRemovalWatcher._parse_log(
        {
            "address": pool.address,
            "transactionHash": "0xtx-mint",
            "blockNumber": "0x64",
            "logIndex": "0x1",
            "topics": [TRANSFER_TOPIC, ZERO_TOPIC, "0x" + "1" * 64],
        },
        {pool.address.lower(): pool},
    )
    assert parsed is None
    assert asset.id == pool.base_asset_id
