"""Task 0: snapshot-coverage precondition.

The label programme presumes forward windows are actually observed: the
density rule in ``forecast.labels.forward_evidence_sufficient`` needs
>= ceil(window / 6h) snapshots in the forward window (4 for 24h), one of
them in the final quartile. These tests pin:

- coverage is judged per decision point with that same rule (no parallel,
  weaker definition),
- snapshots observed after ``as_of`` never count (point-in-time),
- a scan cycle snapshots every asset with an open forecast/score forward
  window through the existing pair path, including assets the refresh
  rotation never reaches (it requires a prior snapshot).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

import ingestion.service as ingestion_service
from common.config import get_settings
from common.time import ensure_utc, floor_to_hour, utc_now
from ingestion.coverage import snapshot_coverage
from ingestion.service import IngestionService
from storage import models
from storage.repository import (
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

H = timedelta(hours=1)
T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
QUOTE_ADDRESS = "USDC111111111111111111111111111111111111"


def _seed_asset(session, *, address: str, symbol: str) -> tuple[models.Asset, models.Pair]:
    chain, _source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=address,
        symbol=symbol,
        name=f"{symbol} Fixture",
        first_seen_at=T0 - timedelta(days=1),
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address=QUOTE_ADDRESS,
        symbol="USDC",
        name="USD Coin",
        first_seen_at=T0 - timedelta(days=365),
    )
    _pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=f"Pair{address[:20]}",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0 - timedelta(days=1),
    )
    session.commit()
    return asset, pair


def _add_snapshot(
    session,
    *,
    pair: models.Pair,
    ts: datetime,
    observed_at: datetime | None = None,
) -> None:
    _chain, source = seed_reference(session)
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=ts,
        observed_at=observed_at if observed_at is not None else ts,
        price_usd=1.0,
        volume_usd=10_000,
        buys=20,
        sells=5,
        trades=25,
    )
    session.commit()


def _add_forecast(session, *, asset: models.Asset, decision_ts: datetime) -> None:
    session.add(
        models.Forecast(
            asset_id=asset.id,
            decision_ts=decision_ts,
            observed_at=decision_ts,
            p_ignition_24h=0.5,
            p_collapse_24h=0.5,
            model_version="coverage-test",
        )
    )
    session.commit()


def _add_score(session, *, asset: models.Asset, decision_ts: datetime) -> None:
    session.add(
        models.Score(
            asset_id=asset.id,
            decision_ts=decision_ts,
            observed_at=decision_ts,
            hype=0.5,
            ethos=0.5,
            risk=0.5,
            liquidity_access=0.5,
            manipulation=0.5,
            confidence=0.5,
            uncertainty=0.5,
            catalyst=0.5,
            exit_risk=0.5,
            research_priority=0.5,
            risk_band="YELLOW",
            confidence_source="fallback",
            model_version="coverage-test",
        )
    )
    session.commit()


def test_coverage_counts_density_with_the_label_rule(session):
    # Asset A: decision at T0, snapshots at +1h, +7h, +13h, +20h
    # (4 in window, one in the final quartile) -> meets density.
    # Asset B: decision at T0, snapshots at +1h, +2h, +3h
    # (3 in window, none in the final quartile) -> does not.
    asset_a, pair_a = _seed_asset(
        session, address="TokenA111111111111111111111111111111", symbol="AAA"
    )
    asset_b, pair_b = _seed_asset(
        session, address="TokenB111111111111111111111111111111", symbol="BBB"
    )
    _add_forecast(session, asset=asset_a, decision_ts=T0)
    _add_score(session, asset=asset_b, decision_ts=T0)
    for offset in (1, 7, 13, 20):
        _add_snapshot(session, pair=pair_a, ts=T0 + offset * H)
    for offset in (1, 2, 3):
        _add_snapshot(session, pair=pair_b, ts=T0 + offset * H)

    report = snapshot_coverage(session, settings=get_settings(), as_of=T0 + 48 * H)

    assert report.assets_measured == 2
    assert report.assets_meeting_density == 1
    assert report.pass_share == pytest.approx(0.5)
    # Per-day density over the 48h measured span: A has 4 snapshots (2/day),
    # B has 3 (1.5/day).
    assert report.median_snapshots_per_asset_day == pytest.approx(1.75)
    assert report.p10_snapshots_per_asset_day == pytest.approx(1.55)


def test_coverage_ignores_snapshots_observed_after_as_of(session):
    # A snapshot backfilled with observed_at after as_of must not
    # retroactively make a thin window dense (point-in-time or it
    # didn't happen).
    asset_a, pair_a = _seed_asset(
        session, address="TokenA111111111111111111111111111111", symbol="AAA"
    )
    asset_c, pair_c = _seed_asset(
        session, address="TokenC111111111111111111111111111111", symbol="CCC"
    )
    as_of = T0 + 48 * H
    _add_forecast(session, asset=asset_a, decision_ts=T0)
    _add_forecast(session, asset=asset_c, decision_ts=T0)
    for offset in (1, 7, 13, 20):
        _add_snapshot(session, pair=pair_a, ts=T0 + offset * H)
    # Asset C has the same ts layout, but the final-quartile snapshot was
    # only observed after as_of: on ts alone it would pass, so a pass here
    # proves the observed_at filter is missing.
    for offset in (1, 7, 13):
        _add_snapshot(session, pair=pair_c, ts=T0 + offset * H)
    _add_snapshot(session, pair=pair_c, ts=T0 + 20 * H, observed_at=as_of + H)

    report = snapshot_coverage(session, settings=get_settings(), as_of=as_of)

    assert report.assets_measured == 2
    assert report.assets_meeting_density == 1
    assert report.pass_share == pytest.approx(0.5)


def test_coverage_empty_scope_is_zero_not_error(session):
    report = snapshot_coverage(session, settings=get_settings(), as_of=T0 + 48 * H)

    assert report.assets_measured == 0
    assert report.assets_meeting_density == 0
    assert report.pass_share == 0.0
    assert report.median_snapshots_per_asset_day == 0.0
    assert report.p10_snapshots_per_asset_day == 0.0


def _pair_payload(*, pair_address: str, token_address: str, symbol: str) -> dict:
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": pair_address,
        "pairCreatedAt": int(datetime(2026, 5, 1, tzinfo=UTC).timestamp() * 1000),
        "baseToken": {"address": token_address, "symbol": symbol, "name": f"{symbol} Token"},
        "quoteToken": {"address": QUOTE_ADDRESS, "symbol": "USDC", "name": "USD Coin"},
        "priceUsd": "1.23",
        "volume": {"h1": 10000},
        "liquidity": {"usd": 80000, "base": 1000, "quote": 80000},
        "txns": {"h1": {"buys": 12, "sells": 3}},
    }


class _FakeDexScreener:
    """Stand-in for DexScreenerClient: canned token-pairs per token address."""

    def __init__(self, payloads: dict[str, list[dict]]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, str]] = []

    def token_pairs(self, chain_slug: str, token_address: str) -> list[dict]:
        self.calls.append((chain_slug, token_address))
        return self.payloads.get(token_address, [])

    def close(self) -> None:
        return None


class _RpcPool:
    def probe_down_endpoints(self, _probe) -> int:
        return 0

    def dispatch_alerts(self, _notify, *, cooldown_seconds: float) -> int:
        return 0


def _stub_scan_except_snapshot_paths(monkeypatch, service: IngestionService) -> None:
    """Neutralise every run_once stage except refresh + decision snapshots."""
    for name in (
        "_ingest_dexscreener_profiles",
        "_ingest_dexscreener_boosts",
        "_ingest_geckoterminal_new_pools",
        "_catch_up_evm_first_buys",
        "_record_first_buy_health",
        "_ingest_birdeye_solana",
        "_ingest_solana_holder_snapshots",
        "_ingest_solana_safety_snapshots",
        "_ingest_evm_holder_snapshots",
    ):
        monkeypatch.setattr(service, name, lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(service, "_run_data_quality_check", lambda *_a, **_k: {})
    monkeypatch.setattr(service, "_run_contract_analysis", lambda *_a, **_k: {})
    monkeypatch.setattr(ingestion_service, "run_mempool", lambda *_a, **_k: {})
    monkeypatch.setattr(ingestion_service, "run_narrative", lambda *_a, **_k: 0)
    monkeypatch.setattr(ingestion_service, "extract_catalysts", lambda *_a, **_k: 0)
    monkeypatch.setattr(ingestion_service, "alert_upcoming_catalysts", lambda *_a, **_k: 0)
    monkeypatch.setattr(ingestion_service, "run_lifecycle", lambda *_a, **_k: 0)
    monkeypatch.setattr(ingestion_service, "run_forecast_cycle", lambda *_a, **_k: {})
    monkeypatch.setattr(ingestion_service, "score_current_assets", lambda *_a, **_k: [])
    monkeypatch.setattr(ingestion_service, "run_notifier", lambda *_a, **_k: {})
    monkeypatch.setattr(ingestion_service, "get_rpc_pool", lambda _chain: _RpcPool())
    monkeypatch.setattr(ingestion_service, "persist_pool_snapshots", lambda *_a, **_k: 0)
    monkeypatch.setattr(ingestion_service, "record_pool_health", lambda *_a, **_k: None)
    monkeypatch.setattr(ingestion_service.LiquidityRemovalWatcher, "scan", lambda *_a, **_k: {})
    monkeypatch.setattr(ingestion_service.PrelaunchQueue, "scan", lambda *_a, **_k: [])
    monkeypatch.setattr(ingestion_service.IgnitionRadar, "scan", lambda *_a, **_k: {})
    monkeypatch.setattr(ingestion_service.FingerprintEngine, "learn", lambda *_a, **_k: {})
    monkeypatch.setattr(ingestion_service.FingerprintEngine, "assess", lambda *_a, **_k: [])


def test_scan_cycle_snapshots_forecasted_assets(session, monkeypatch):
    # A forecasted asset whose pair is not in the default poll set: the
    # refresh rotation requires a prior snapshot, so a never-snapshotted
    # pair is invisible to it. One run_once cycle must still write a
    # market snapshot for the pair, through the existing pair path.
    asset, pair = _seed_asset(session, address="TokenF111111111111111111111111111111", symbol="FFF")
    other, other_pair = _seed_asset(
        session, address="TokenG111111111111111111111111111111", symbol="GGG"
    )
    _add_forecast(session, asset=asset, decision_ts=utc_now() - H)
    pair_address = session.scalar(select(models.Pair.id).where(models.Pair.id == pair.id))
    assert pair_address is not None
    stored_pair_address = f"Pair{asset.address[:20]}"
    fake = _FakeDexScreener(
        {
            asset.address: [
                _pair_payload(
                    pair_address=stored_pair_address,
                    token_address=asset.address,
                    symbol="FFF",
                )
            ]
        }
    )
    monkeypatch.setattr(ingestion_service, "DexScreenerClient", lambda: fake)
    service = IngestionService()
    _stub_scan_except_snapshot_paths(monkeypatch, service)

    cycle_start = utc_now()
    result = service.run_once(session)

    assert result["errors"] == []
    assert ("solana", asset.address) in fake.calls
    snap_ts = session.scalar(
        select(models.MarketSnapshot.ts).where(models.MarketSnapshot.pair_id == pair.id)
    )
    assert snap_ts is not None
    assert floor_to_hour(cycle_start) <= ensure_utc(snap_ts) <= cycle_start + H
    assert result["decision_snapshots"] >= 1
    # The undecided asset is not in the decision set: no fetch, no snapshot.
    assert ("solana", other.address) not in fake.calls
    other_count = session.scalar(
        select(func.count())
        .select_from(models.MarketSnapshot)
        .where(models.MarketSnapshot.pair_id == other_pair.id)
    )
    assert other_count == 0
