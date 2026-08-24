from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

import ingestion.service as ingestion_service
from ingestion.service import IngestionService
from ops.archive import LocalArchiveStore
from storage import models
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
