from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.config import Settings
from features.definitions import FEATURE_NAMES
from features.factory import FeatureFactory
from features.lake import LAKE_FEATURE_NAMES, LakeFeatureFactory
from ops.archive import LocalArchiveStore, RawEvidenceCompactor
from storage import models
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    store_raw_evidence,
    upsert_asset,
    upsert_pool_and_pair,
)

DECISION = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
BASE_ADDRESS = "TokenLake11111111111111111111111111111111"
PAIR_ADDRESS = "PairLake1111111111111111111111111111111111"


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        archive_enabled=True,
        archive_backend="local",
        archive_local_dir=str(tmp_path),
        archive_compact_after_hours=0.0,
        archive_retention_days=30,
        archive_batch_size=5_000,
    )


def _pool_payload(
    *,
    pair_address: str,
    price: float,
    volume: float,
    liquidity: float,
    buys: int,
    sells: int,
    created_at: datetime,
) -> dict:
    """A GeckoTerminal ``new_pools`` item shaped like the live API payload.

    The h1 window drives the normalization (``h1 or h24 or m5`` precedence),
    exactly as ``normalize_geckoterminal_pool`` resolves it.
    """
    return {
        "id": f"solana_{pair_address}",
        "type": "pool",
        "attributes": {
            "address": pair_address,
            "name": "LAKE / WETH",
            "pool_created_at": created_at.isoformat().replace("+00:00", "Z"),
            "base_token_price_usd": str(price),
            "volume_usd": {
                "h1": str(volume),
                "h24": str(volume * 6),
                "m5": str(round(volume / 2, 2)),
            },
            "reserve_in_usd": str(liquidity),
            "transactions": {
                "h1": {"buys": buys, "sells": sells},
                "h24": {"buys": buys * 6, "sells": sells * 6},
                "m5": {"buys": max(1, buys // 2), "sells": max(1, sells // 2)},
            },
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_{BASE_ADDRESS}", "type": "token"}},
            "quote_token": {
                "data": {
                    "id": "solana_So11111111111111111111111111111111111111112",
                    "type": "token",
                }
            },
            "dex": {"data": {"id": "solana_raydium", "type": "dex"}},
        },
    }


def _seed_sql_and_lake(session, tmp_path) -> models.Asset:
    """Seed identical observations into the SQL tables AND the Parquet lake.

    SQL path: normalized MarketSnapshot/LiquiditySnapshot rows. Lake path: the
    GeckoTerminal-shaped raw evidence payloads that produced those rows,
    compacted into partitioned Parquet — the production flow.
    """
    chain = get_or_create_chain(
        session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
    )
    source = get_or_create_source(
        session,
        name="geckoterminal",
        source_type="market_data",
        tier="venue",
        base_url="https://api.geckoterminal.com",
    )
    created_at = DECISION - timedelta(hours=6)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=BASE_ADDRESS,
        symbol="LAKE",
        name="Lake Token",
        first_seen_at=created_at,
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="So11111111111111111111111111111111111111112",
        symbol="WETH",
        name="Wrapped Ether",
        first_seen_at=created_at - timedelta(days=365),
    )
    pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=PAIR_ADDRESS,
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=created_at,
    )

    # price arc over the trailing 6h + a fresh print 1 minute before decision.
    arc = [
        (6, 1.00, 5_000, 12, 6),
        (5, 1.10, 7_000, 18, 7),
        (4, 1.05, 6_000, 14, 9),
        (3, 1.20, 12_000, 30, 10),
        (2, 1.30, 20_000, 44, 12),
        (1, 1.25, 18_000, 38, 14),
        (1 / 60.0, 1.40, 25_000, 52, 15),
    ]
    for hours_ago, price, volume, buys, sells in arc:
        observed_at = DECISION - timedelta(hours=hours_ago)
        raw = store_raw_evidence(
            session,
            source=source,
            payload={
                "chain": "solana",
                "new_pools": [
                    _pool_payload(
                        pair_address=PAIR_ADDRESS,
                        price=price,
                        volume=volume,
                        liquidity=50_000 + hours_ago * 2_000,
                        buys=buys,
                        sells=sells,
                        created_at=created_at,
                    )
                ],
            },
            observed_at=observed_at,
        )
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=observed_at.replace(minute=0, second=0, microsecond=0),
            observed_at=observed_at,
            price_usd=price,
            volume_usd=volume,
            buys=buys,
            sells=sells,
            raw_evidence_id=raw.id,
        )
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=observed_at.replace(minute=0, second=0, microsecond=0),
            observed_at=observed_at,
            reserve_usd=50_000 + hours_ago * 2_000,
            raw_evidence_id=raw.id,
        )
    session.flush()

    settings = _settings(tmp_path)
    RawEvidenceCompactor(
        store=LocalArchiveStore(tmp_path), settings=settings
    ).compact(session, DECISION)
    session.flush()
    return asset


def test_lake_feature_names_are_subset_of_full_set() -> None:
    assert set(LAKE_FEATURE_NAMES) <= set(FEATURE_NAMES)
    assert len(LAKE_FEATURE_NAMES) == len(set(LAKE_FEATURE_NAMES))


def test_lake_read_path_matches_sql_read_path(session, tmp_path) -> None:
    """The DuckDB lake path computes the market/liquidity block identically to
    the SQL path — the parity contract. Values AND missing flags must match."""
    asset = _seed_sql_and_lake(session, tmp_path)
    session.commit()

    sql_values = {
        value.name: value
        for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }
    store = LocalArchiveStore(tmp_path)
    lake_values = LakeFeatureFactory(
        store=store, settings=_settings(tmp_path)
    ).build_for_asset(asset_address=asset.address, decision_ts=DECISION)

    assert set(lake_values) == set(LAKE_FEATURE_NAMES)
    for name in LAKE_FEATURE_NAMES:
        sql_feature = sql_values[name]
        lake_feature = lake_values[name]
        assert (
            lake_feature.missing == sql_feature.missing
        ), f"{name}: missing flag diverged ({lake_feature.missing} vs {sql_feature.missing})"
        if not sql_feature.missing:
            assert lake_feature.value == pytest.approx(
                sql_feature.value, rel=1e-6
            ), f"{name}: value diverged ({lake_feature.value} vs {sql_feature.value})"
        else:
            assert lake_feature.value == 0.0


def test_lake_read_path_reports_missing_on_empty_lake(session, tmp_path) -> None:
    """No evidence in the lake -> the market block reports honest missing."""
    lake_values = LakeFeatureFactory(
        store=LocalArchiveStore(tmp_path), settings=_settings(tmp_path)
    ).build_for_asset(asset_address=BASE_ADDRESS, decision_ts=DECISION)
    assert set(lake_values) == set(LAKE_FEATURE_NAMES)
    assert all(value.missing for value in lake_values.values())


def test_lake_read_path_ignores_other_assets(session, tmp_path) -> None:
    """Only evidence for the requested base address enters the series."""
    _seed_sql_and_lake(session, tmp_path)
    session.commit()
    other = "TokenOther11111111111111111111111111111111"
    lake_values = LakeFeatureFactory(
        store=LocalArchiveStore(tmp_path), settings=_settings(tmp_path)
    ).build_for_asset(asset_address=other, decision_ts=DECISION)
    assert all(value.missing for value in lake_values.values())


def test_lake_reconstructs_series_rows(session, tmp_path) -> None:
    """The reconstructed series has one deduped row per (pair, hour) and the
    pair-created time from the pool payload."""
    asset = _seed_sql_and_lake(session, tmp_path)
    session.commit()
    factory = LakeFeatureFactory(
        store=LocalArchiveStore(tmp_path), settings=_settings(tmp_path)
    )
    series = factory._series_from_lake(asset.address, DECISION)
    # The 1-min print floors into the same hour as the 1h print, so first-wins
    # dedup collapses the arc to one row per (pair, hour) — 6 in total.
    assert len(series.market_rows) == 6
    assert len(series.liquidity_rows) == 6
    assert series.pair_created_at is not None
    assert series.pair_created_at == DECISION - timedelta(hours=6)
    # First-wins dedup: same (pair, hour) collapses to one row.
    hours = {row.ts.replace(minute=0, second=0, microsecond=0) for row in series.market_rows}
    assert len(hours) == len(series.market_rows)
    # The lookbacks resolve (the hour boundary lands exactly on the previous
    # print, so 1h return is 0 by construction — the SQL path agrees). The
    # volume spike from the last print makes acceleration non-trivial.
    market_block = factory.build_for_asset(asset_address=asset.address, decision_ts=DECISION)
    assert not market_block["one_hour_return"].missing
    assert not market_block["volume_acceleration"].missing
    assert market_block["volume_acceleration"].value > 1.5
