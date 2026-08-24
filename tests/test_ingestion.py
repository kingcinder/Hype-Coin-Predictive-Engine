from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

import ingestion.service as ingestion_service
from common.time import ensure_utc
from ingestion.contract_analyzer import ContractAnalysis
from ingestion.service import IngestionService, _analysis_findings
from ops.archive import LocalArchiveStore
from storage import models
from storage.repository import (
    get_or_create_chain,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_market_asset, seed_reference


def test_dexscreener_pair_ingest_is_idempotent(session) -> None:
    seed_reference(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    payload = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "PairABC",
        "pairCreatedAt": int(datetime(2026, 5, 1, tzinfo=UTC).timestamp() * 1000),
        "baseToken": {"address": "TokenABC", "symbol": "ABC", "name": "ABC Token"},
        "quoteToken": {"address": "USDC", "symbol": "USDC", "name": "USD Coin"},
        "priceUsd": "1.23",
        "volume": {"h1": 10000},
        "liquidity": {"usd": 80000, "base": 1000, "quote": 80000},
        "txns": {"h1": {"buys": 12, "sells": 3}},
    }
    service = IngestionService()
    assert service._store_dexscreener_pair(session, source, payload) is True
    assert service._store_dexscreener_pair(session, source, payload) is True
    session.commit()

    assert (
        session.scalar(select(models.Asset).where(models.Asset.address == "TokenABC")).symbol
        == "ABC"
    )
    assert session.scalar(select(func.count()).select_from(models.MarketSnapshot)) == 1
    assert session.scalar(select(func.count()).select_from(models.LiquiditySnapshot)) == 1


def test_geckoterminal_pool_ingest_creates_point_in_time_rows(session, monkeypatch) -> None:
    service = IngestionService()
    service.ensure_reference_data(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "geckoterminal"))
    assert source is not None
    observed_at = datetime(2026, 5, 1, 12, 15, 30, tzinfo=UTC)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: observed_at)
    payload = {
        "id": "base_0xa3255cffb9b9aeb6363d643b1040a24d5124bac4",
        "type": "pool",
        "attributes": {
            "address": "0xa3255cffb9b9aeb6363d643b1040a24d5124bac4",
            "name": "OpenClaw / WETH",
            "pool_created_at": "2026-05-01T11:45:13Z",
            "base_token_price_usd": "0.00000351697615196928",
            "volume_usd": {"h1": "1234.50", "h24": "4567.80"},
            "reserve_in_usd": "59609.2288931033",
            "transactions": {"h1": {"buys": 7, "sells": 2, "buyers": 7, "sellers": 2}},
        },
        "relationships": {
            "base_token": {
                "data": {"id": "base_0xd413e8bbeeb0f3d939042f0a7ea9af8e9cdeb58c", "type": "token"}
            },
            "quote_token": {
                "data": {"id": "base_0x4200000000000000000000000000000000000006", "type": "token"}
            },
            "dex": {"data": {"id": "uniswap-v2-base", "type": "dex"}},
        },
    }
    assert service._store_geckoterminal_pool(session, source, payload) is True
    assert service._store_geckoterminal_pool(session, source, payload) is True
    session.commit()

    asset = session.scalar(
        select(models.Asset).where(
            models.Asset.address == "0xd413e8bbeeb0f3d939042f0a7ea9af8e9cdeb58c"
        )
    )
    assert asset is not None
    assert asset.symbol == "OpenClaw"
    assert session.scalar(select(func.count()).select_from(models.Pool)) == 1
    assert session.scalar(select(func.count()).select_from(models.Pair)) == 1
    assert session.scalar(select(func.count()).select_from(models.MarketSnapshot)) == 1
    assert session.scalar(select(func.count()).select_from(models.LiquiditySnapshot)) == 1
    assert session.scalar(select(func.count()).select_from(models.RawEvidenceItem)) == 1


def test_solana_holder_snapshot_ingest_is_bounded_and_idempotent(
    session, monkeypatch
) -> None:
    asset = seed_market_asset(session)
    service = IngestionService()
    service.ensure_reference_data(session)
    service.settings.solana_holder_scan_limit = 1
    observed_at = datetime(2026, 5, 1, 13, 30, tzinfo=UTC)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: observed_at)

    class FakeSolanaRpcClient:
        def close(self) -> None:
            return None

        def get_token_supply(self, mint: str) -> float:
            assert mint == asset.address
            return 100.0

        def get_token_largest_accounts(self, mint: str):
            assert mint == asset.address
            return [
                {"address": "holder-a", "uiAmountString": "70"},
                {"address": "holder-b", "uiAmount": 20},
            ]

    monkeypatch.setattr(ingestion_service, "SolanaRpcClient", FakeSolanaRpcClient)

    assert service._ingest_solana_holder_snapshots(session) == 2
    assert service._ingest_solana_holder_snapshots(session) == 2
    session.commit()

    holders = session.scalars(select(models.Holder).where(models.Holder.asset_id == asset.id)).all()
    assert len(holders) == 2
    assert round(sum(holder.pct_supply or 0.0 for holder in holders), 6) == 0.9
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "source:solana_holders")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert health is not None
    assert health.state == "ok"
    assert "2 holder rows across 1 assets" in (health.message or "")


class _FakeStage:
    """Network-free stand-ins for the scan pipeline stages."""

    def scan(self, session, decision_ts=None):
        return {}

    def learn(self, session):
        return {}

    def assess(self, session):
        return []


def test_analysis_findings_maps_analysis_to_flagged_findings() -> None:
    """The deterministic findings list covers the analyzer's risk dimensions
    (honeypot patterns, mint authority, pause function, ownership not
    renounced, rug deployer) and stays empty for a clean analysis."""
    assert _analysis_findings(ContractAnalysis()) == []

    findings = _analysis_findings(
        ContractAnalysis(
            suspicious_flags=5,
            reasons=[
                "Contract contains allowance-based sell block",
                "Contract has mint function (supply can be inflated)",
            ],
            has_mint_function=True,
            has_pause_function=True,
            ownership_renounced=False,
            deployer_known_rug=True,
        )
    )
    assert findings == [
        {"flag_type": "honeypot", "severity": "high"},
        {"flag_type": "mint_authority", "severity": "warning"},
        {"flag_type": "pause_function", "severity": "warning"},
        {"flag_type": "ownership_not_renounced", "severity": "warning"},
        {"flag_type": "rug_deployer", "severity": "critical"},
    ]
    # Ownership renounced (True) or unknown (None) is NOT a finding.
    assert _analysis_findings(ContractAnalysis(ownership_renounced=True)) == []
    assert _analysis_findings(ContractAnalysis(ownership_renounced=None)) == []


def test_contract_analysis_persists_evidence_and_flags(session, monkeypatch) -> None:
    """_run_contract_analysis persists each finding as point-in-time raw
    evidence (the contract_analysis payload) AND one evidence-backed
    ContractFlag row, so the SQL count and the lake replay see the full flag
    set — and re-analysis is idempotent (no duplicate flags/evidence)."""
    chain, _ = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Token111111111111111111111111111111111111",
        symbol="HYPE",
        name="Hype Fixture",
        first_seen_at=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
    )
    session.flush()
    service = IngestionService()
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    def fake_analyze(address, chain="ethereum", *, http=None):
        assert address == asset.address
        return ContractAnalysis(
            suspicious_flags=2,
            reasons=["Contract has mint function (supply can be inflated)"],
            has_mint_function=True,
            ownership_renounced=False,
        )

    monkeypatch.setattr(ingestion_service, "analyze_contract", fake_analyze)

    result = service._run_contract_analysis(session, decision_ts)
    session.flush()
    assert result["analyzed"] == 1
    assert result["flagged"] == 1
    expected_findings = [
        {"flag_type": "mint_authority", "severity": "warning"},
        {"flag_type": "ownership_not_renounced", "severity": "warning"},
    ]

    evidence = [
        row
        for row in session.scalars(select(models.RawEvidenceItem)).all()
        if "contract_analysis" in (row.payload or {})
    ]
    assert len(evidence) == 1
    payload = evidence[0].payload["contract_analysis"]
    assert payload["asset_address"] == asset.address
    assert payload["chain"] == "solana"
    assert payload["findings"] == expected_findings

    contract = session.scalar(
        select(models.Contract).where(models.Contract.asset_id == asset.id)
    )
    assert contract is not None
    flags = session.scalars(
        select(models.ContractFlag).where(models.ContractFlag.contract_id == contract.id)
    ).all()
    assert {flag.flag_type for flag in flags} == {
        "mint_authority",
        "ownership_not_renounced",
    }
    assert all(flag.severity == "warning" for flag in flags)
    assert all(ensure_utc(flag.observed_at) == decision_ts for flag in flags)
    assert all(flag.evidence_id == evidence[0].id for flag in flags)

    # Re-analysis of the same asset is idempotent: evidence dedupes on content
    # hash and existing (contract, flag_type) rows are never duplicated.
    service._run_contract_analysis(session, decision_ts)
    session.flush()
    flags_after = session.scalars(
        select(models.ContractFlag).where(models.ContractFlag.contract_id == contract.id)
    ).all()
    assert len(flags_after) == len(flags)
    assert len(evidence) == 1


def test_evm_holder_snapshot_ingest_is_bounded_and_idempotent(
    session, monkeypatch
) -> None:
    """The EVM holder scan (Blockscout) stores the same evidence shape as the
    Solana path on the evm_holders chain_rpc source, writes Holder rows with
    pct-of-supply, reports source:evm_holders health, and is idempotent on a
    repeat scan."""
    chain = get_or_create_chain(
        session, "base", name="Base", vm_type="evm", native_symbol="ETH"
    )
    service = IngestionService()
    service.ensure_reference_data(session)
    service.settings.evm_holder_scan_limit = 1
    service.settings.evm_holder_rpc_pause_seconds = 0.0
    observed_at = datetime(2026, 5, 1, 13, 30, tzinfo=UTC)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: observed_at)

    created = observed_at - timedelta(hours=2)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="0x1111111111111111111111111111111111111111",
        symbol="EVMT",
        name="EVM Token",
        first_seen_at=created,
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="0x2222222222222222222222222222222222222222",
        symbol="WETH",
        name="Wrapped Ether",
        first_seen_at=created - timedelta(days=365),
    )
    upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="uniswap-v2-base",
        pair_address="0x3333333333333333333333333333333333333333",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=created,
    )
    session.flush()

    class FakeEVMHolderClient:
        def __init__(self, chain_slug: str) -> None:
            assert chain_slug == "base"

        def close(self) -> None:
            return None

        def token_supply(self, address: str) -> float:
            assert address == asset.address
            return 100.0

        def top_holders(self, address: str):
            assert address == asset.address
            return [
                {"address": "0xaaa11111111111111111111111111111111111111", "uiAmountString": "70"},
                {"address": "0xbbb11111111111111111111111111111111111111", "uiAmountString": "20"},
            ]

    monkeypatch.setattr(ingestion_service, "EVMHolderClient", FakeEVMHolderClient)

    assert service._ingest_evm_holder_snapshots(session) == 2
    assert service._ingest_evm_holder_snapshots(session) == 2
    session.commit()

    holders = session.scalars(
        select(models.Holder).where(models.Holder.asset_id == asset.id)
    ).all()
    assert len(holders) == 2
    assert round(sum(holder.pct_supply or 0.0 for holder in holders), 6) == 0.9

    evidence = [
        row
        for row in session.scalars(select(models.RawEvidenceItem)).all()
        if "mint" in (row.payload or {})
    ]
    assert len(evidence) == 1
    assert evidence[0].payload["mint"] == asset.address
    assert evidence[0].payload["chain"] == "base"
    assert evidence[0].payload["supply"] == pytest.approx(100.0)
    assert evidence[0].payload["largest_accounts"] == [
        {"address": "0xaaa11111111111111111111111111111111111111", "uiAmountString": "70"},
        {"address": "0xbbb11111111111111111111111111111111111111", "uiAmountString": "20"},
    ]

    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "source:evm_holders")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert health is not None
    assert health.state == "ok"
    assert "2 holder rows across 1 chains" in (health.message or "")


def test_scan_never_touches_archive(session, tmp_path, monkeypatch) -> None:
    """The ingestion worker must not compact: it reports the archive stage as
    skipped (archive=0 in scan results) and writes no parquet files — the
    retention autopilot owns compaction on its cadence."""
    seed_reference(session)
    service = IngestionService()

    monkeypatch.setattr(service, "_ingest_dexscreener_profiles", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_dexscreener_boosts", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_geckoterminal_new_pools", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_birdeye_solana", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_solana_holder_snapshots", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_evm_holder_snapshots", lambda session: 0)
    monkeypatch.setattr(service, "_run_data_quality_check", lambda session, decision_ts: {})
    monkeypatch.setattr(service, "_run_contract_analysis", lambda session, decision_ts: {})
    monkeypatch.setattr(ingestion_service, "run_mempool", lambda session, decision_ts=None: {})
    monkeypatch.setattr(
        ingestion_service, "run_narrative", lambda session, decision_ts=None: {}
    )
    monkeypatch.setattr(
        ingestion_service, "extract_catalysts", lambda session, decision_ts=None: 0
    )
    monkeypatch.setattr(
        ingestion_service,
        "alert_upcoming_catalysts",
        lambda session, decision_ts=None: 0,
    )
    monkeypatch.setattr(
        ingestion_service,
        "run_lifecycle",
        lambda session, decision_ts=None: {"events": 0, "assets": 0},
    )
    monkeypatch.setattr(
        ingestion_service,
        "run_forecast_if_due",
        lambda session, decision_ts=None: {"forecasts": 0},
    )
    monkeypatch.setattr(
        ingestion_service,
        "score_current_assets",
        lambda session, decision_ts=None, asset_ids=None: [],
    )
    monkeypatch.setattr(
        ingestion_service, "run_notifier", lambda session, decision_ts=None: {"sent": 0}
    )
    monkeypatch.setattr(ingestion_service, "LiquidityRemovalWatcher", _FakeStage)
    monkeypatch.setattr(ingestion_service, "PrelaunchQueue", _FakeStage)
    monkeypatch.setattr(ingestion_service, "IgnitionRadar", _FakeStage)
    monkeypatch.setattr(ingestion_service, "FingerprintEngine", _FakeStage)

    result = service.run_once(session)

    assert result["archive"] == {"skipped": True, "partitions": 0, "compacted": 0}
    assert LocalArchiveStore(tmp_path).list_objects("evidence") == []
    scan = session.scalar(
        select(models.ScanResult).order_by(models.ScanResult.ts.desc()).limit(1)
    )
    assert scan is not None
    assert scan.state == "ok"
    assert scan.archive == 0
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "worker")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert "retention autopilot" in (health.message or "")
