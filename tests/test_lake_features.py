from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from common.config import Settings, get_settings
from features.definitions import FEATURE_NAMES
from features.factory import FeatureFactory, build_and_persist_features
from features.lake import LAKE_FEATURE_NAMES, LakeFeatureFactory
from ops.archive import LocalArchiveStore, RawEvidenceCompactor
from storage import models
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_holder_once,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    store_raw_evidence,
    upsert_asset,
    upsert_contract,
    upsert_pool_and_pair,
)

DECISION = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
BASE_ADDRESS = "TokenLake11111111111111111111111111111111"
PAIR_ADDRESS = "PairLake1111111111111111111111111111111111"

# Holder-snapshot arc: 5 accounts two hours before the decision, 7 accounts at
# the decision hour. SQL turns each snapshot into Holder rows at the floored
# hour; the lake reconstructs the same numbers from the RPC evidence.
SNAPSHOT_A = [
    ("wallet-a", 100.0),
    ("wallet-b", 200.0),
    ("wallet-c", 300.0),
    ("wallet-d", 400.0),
    ("wallet-e", 500.0),
]
SNAPSHOT_B = SNAPSHOT_A + [("wallet-f", 600.0), ("wallet-g", 700.0)]
SUPPLY = 10_000.0


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


def _seed_holder_evidence(
    session,
    asset,
    *,
    with_sql_rows: bool,
    source_name: str = "solana_rpc",
    mint_address: str | None = None,
    snapshots: list[tuple[int, list[tuple[str, float]]]] | None = None,
) -> None:
    """Seed the holder-snapshot RPC evidence (and, on the SQL side, the
    Holder rows it produces in production) for the parity comparison.

    ``source_name`` drives which chain_rpc source the evidence lands on
    (``solana_rpc`` for Solana RPC, ``evm_holders`` for Blockscout) — the lake
    reconstruction is chain-agnostic and must serve both. ``snapshots`` are
    (hours_before_decision, accounts) pairs, defaulting to the Solana arc.
    """
    source = get_or_create_source(
        session,
        name=source_name,
        source_type="chain_rpc",
        tier="chain",
        base_url=None,
    )
    mint = mint_address or asset.address
    snapshots = snapshots or [(2, SNAPSHOT_A), (0, SNAPSHOT_B)]
    for hours_ago, accounts in snapshots:
        observed = DECISION - timedelta(hours=hours_ago)
        store_raw_evidence(
            session,
            source=source,
            payload={
                "asset_id": asset.id,
                "mint": mint,
                "supply": SUPPLY,
                "largest_accounts": [
                    {"address": address, "uiAmountString": str(balance)}
                    for address, balance in accounts
                ],
            },
            observed_at=observed,
        )
        if with_sql_rows:
            ts = observed.replace(minute=0, second=0, microsecond=0)
            for address, balance in accounts:
                insert_holder_once(
                    session,
                    asset_id=asset.id,
                    wallet_address=address,
                    source_id=source.id,
                    ts=ts,
                    observed_at=observed,
                    balance=balance,
                    pct_supply=balance / SUPPLY,
                )


def _seed_low_liquidity_scan(
    session, source, asset, pair, *, with_sql_rows: bool
) -> None:
    """Seed one GeckoTerminal scan whose pool reserve fell below the discovery
    liquidity threshold. In production that scan creates one ``low_liquidity``
    ContractFlag; the lake must count the same thing from the evidence.

    The scan lands at 08:30, which floors into the same hour as the 08:00 arc
    point, so the market/liquidity series is unchanged (first-wins dedup) —
    only the contract-flag count grows.
    """
    observed = DECISION - timedelta(hours=3, minutes=30)
    raw = store_raw_evidence(
        session,
        source=source,
        payload={
            "chain": "solana",
            "new_pools": [
                _pool_payload(
                    pair_address=PAIR_ADDRESS,
                    price=0.5,
                    volume=100,
                    liquidity=900,
                    buys=1,
                    sells=1,
                    created_at=asset.first_seen_at,
                )
            ],
        },
        observed_at=observed,
    )
    if with_sql_rows:
        ts = observed.replace(minute=0, second=0, microsecond=0)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=observed,
            price_usd=0.5,
            volume_usd=100,
            buys=1,
            sells=1,
            raw_evidence_id=raw.id,
        )
        insert_liquidity_snapshot_once(
            session,
            pool_id=pair.pool_id,
            source_id=source.id,
            ts=ts,
            observed_at=observed,
            reserve_usd=900,
            raw_evidence_id=raw.id,
        )
        contract = upsert_contract(
            session,
            chain_id=asset.chain_id,
            asset_id=asset.id,
            address=asset.address,
            observed_at=observed,
        )
        session.add(
            models.ContractFlag(
                contract_id=contract.id,
                source_id=source.id,
                ts=observed,
                observed_at=observed,
                flag_type="low_liquidity",
                severity="warning",
                evidence_id=raw.id,
                details={"liquidity_usd": 900.0},
            )
        )


def _seed_contract_analysis(session, asset, *, with_sql_rows: bool) -> None:
    """Seed one contract-analysis pass whose findings (mint authority +
    ownership not renounced) become evidence-backed ContractFlag rows in
    production. The lake must count the same findings from the archived
    ``contract_analysis`` evidence — not just low-liquidity scans.
    """
    source = get_or_create_source(
        session,
        name="contract_analysis",
        source_type="chain_rpc",
        tier="chain",
        base_url=None,
    )
    observed = DECISION - timedelta(hours=4)
    findings = [
        {"flag_type": "mint_authority", "severity": "warning"},
        {"flag_type": "ownership_not_renounced", "severity": "warning"},
    ]
    raw = store_raw_evidence(
        session,
        source=source,
        payload={
            "contract_analysis": {
                "chain": "solana",
                "asset_address": asset.address,
                "suspicious_flags": 2,
                "is_honeypot": False,
                "has_mint": True,
                "has_pause": False,
                "ownership_renounced": False,
                "deployer_known_rug": False,
                "reasons": [
                    "Contract has mint function (supply can be inflated)",
                    "Contract ownership not renounced",
                ],
                "findings": findings,
            }
        },
        observed_at=observed,
    )
    if with_sql_rows:
        contract = upsert_contract(
            session,
            chain_id=asset.chain_id,
            asset_id=asset.id,
            address=asset.address,
            observed_at=observed,
        )
        for finding in findings:
            session.add(
                models.ContractFlag(
                    contract_id=contract.id,
                    source_id=source.id,
                    ts=observed,
                    observed_at=observed,
                    flag_type=finding["flag_type"],
                    severity=finding["severity"],
                    evidence_id=raw.id,
                    details=dict(finding),
                )
            )


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
    _seed_holder_evidence(session, asset, with_sql_rows=True)
    _seed_low_liquidity_scan(session, source, asset, pair, with_sql_rows=True)
    _seed_contract_analysis(session, asset, with_sql_rows=True)
    session.flush()

    settings = _settings(tmp_path)
    # Compact half an hour after the decision so the holder snapshot observed
    # exactly at the decision time is archived too (the compactor cut-off is
    # ``decision_ts - compact_after_hours``).
    RawEvidenceCompactor(
        store=LocalArchiveStore(tmp_path), settings=settings
    ).compact(session, DECISION + timedelta(minutes=30))
    session.flush()
    return asset


def _seed_lake_only(session, tmp_path) -> models.Asset:
    """Seed the raw evidence arc into the lake WITHOUT the live normalized
    tables — the ``feature_source="lake"`` replay scenario."""
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
    _, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=PAIR_ADDRESS,
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=created_at,
    )
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
        store_raw_evidence(
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
            observed_at=DECISION - timedelta(hours=hours_ago),
        )
    _seed_holder_evidence(session, asset, with_sql_rows=False)
    _seed_low_liquidity_scan(session, source, asset, pair, with_sql_rows=False)
    _seed_contract_analysis(session, asset, with_sql_rows=False)
    session.flush()
    RawEvidenceCompactor(
        store=LocalArchiveStore(tmp_path), settings=_settings(tmp_path)
    ).compact(session, DECISION + timedelta(minutes=30))
    session.flush()
    return asset


def test_lake_feature_names_are_subset_of_full_set() -> None:
    assert set(LAKE_FEATURE_NAMES) <= set(FEATURE_NAMES)
    assert len(LAKE_FEATURE_NAMES) == len(set(LAKE_FEATURE_NAMES))


def test_lake_persist_for_assets_writes_rows_without_live_tables(
    session, tmp_path
) -> None:
    """The lake persistence path writes Feature rows computed from the
    archived Parquet even when the live normalized tables hold nothing."""
    asset = _seed_lake_only(session, tmp_path)
    store = LocalArchiveStore(tmp_path)

    # The SQL path on this DB (no live snapshots seeded) reports the market
    # series as missing — only pair_age_minutes is derivable from the pair row
    # alone. The lake path must reconstruct them from the archived evidence.
    sql_values = {
        value.name: value
        for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }
    series_names = [
        name
        for name in LAKE_FEATURE_NAMES
        if name not in ("pair_age_minutes", "suspicious_contract_flags")
    ]
    assert all(sql_values[name].missing for name in series_names)
    # The SQL path reports suspicious_contract_flags as 0.0 (never missing).
    assert sql_values["suspicious_contract_flags"].value == 0.0
    assert not sql_values["suspicious_contract_flags"].missing

    factory = LakeFeatureFactory(store=store, settings=_settings(tmp_path))
    output = factory.persist_for_assets(
        session, decision_ts=DECISION, asset_ids=[asset.id]
    )
    session.flush()

    assert set(output[asset.id]) == set(LAKE_FEATURE_NAMES)
    assert not all(value.missing for value in output[asset.id].values())
    rows = session.scalars(
        select(models.Feature).where(models.Feature.asset_id == asset.id)
    ).all()
    assert {row.feature_name for row in rows} == set(LAKE_FEATURE_NAMES)
    by_name = {value.name: value for value in output[asset.id].values()}
    for row in rows:
        expected = by_name[row.feature_name]
        assert row.missing_flag == expected.missing
        if not expected.missing:
            assert row.feature_value == pytest.approx(expected.value, rel=1e-6)


def test_build_and_persist_features_lake_source_switch(
    session, tmp_path, monkeypatch
) -> None:
    """feature_source='lake' replays features entirely from the archived lake
    and persists them via the same upsert path — no live tables touched."""
    asset = _seed_lake_only(session, tmp_path)

    monkeypatch.setenv("ARCHIVE_BACKEND", "local")
    monkeypatch.setenv("ARCHIVE_LOCAL_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        output = build_and_persist_features(
            session, decision_ts=DECISION, asset_ids=[asset.id], feature_source="lake"
        )
    finally:
        get_settings.cache_clear()
    session.flush()

    assert set(output) == {asset.id}
    assert set(output[asset.id]) == set(LAKE_FEATURE_NAMES)
    assert not all(value.missing for value in output[asset.id].values())
    # The on-chain holder and contract-flag features reconstruct from the
    # archived RPC evidence even with no live Holder/ContractFlag tables.
    assert output[asset.id]["holder_count"].value == pytest.approx(7.0)
    assert output[asset.id]["holder_growth"].value == pytest.approx(2.0)
    assert output[asset.id]["top_holder_concentration"].value == pytest.approx(0.28)
    # 1 low-liquidity scan + 2 contract-analysis findings (mint authority,
    # ownership not renounced) all reconstruct from the archived evidence.
    assert output[asset.id]["suspicious_contract_flags"].value == pytest.approx(3.0)
    rows = session.scalars(
        select(models.Feature).where(
            models.Feature.asset_id == asset.id,
            models.Feature.decision_ts == DECISION,
        )
    ).all()
    assert {row.feature_name for row in rows} == set(LAKE_FEATURE_NAMES)


def test_build_and_persist_features_rejects_unknown_source(session) -> None:
    with pytest.raises(ValueError):
        build_and_persist_features(
            session, decision_ts=DECISION, feature_source="bogus"
        )


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
    # On-chain evidence parity: holder count/growth/concentration and the
    # FULL contract-flag count (1 low_liquidity + 2 contract-analysis
    # findings) reconstruct identically from the lake.
    assert lake_values["holder_count"].value == pytest.approx(7.0)
    assert lake_values["holder_growth"].value == pytest.approx(2.0)
    assert lake_values["top_holder_concentration"].value == pytest.approx(0.28)
    assert lake_values["suspicious_contract_flags"].value == pytest.approx(3.0)


def _persisted_feature_snapshot(
    session, asset_id: int, decision_ts: datetime
) -> dict[str, tuple[float, bool, int, float]]:
    """The persisted Feature row fields for an asset at a decision time."""
    rows = session.scalars(
        select(models.Feature).where(
            models.Feature.asset_id == asset_id,
            models.Feature.decision_ts == decision_ts,
        )
    ).all()
    return {
        row.feature_name: (
            float(row.feature_value),
            bool(row.missing_flag),
            int(row.source_count),
            float(row.freshness_score),
        )
        for row in rows
    }


def test_build_and_persist_sql_vs_lake_identical_rows(
    session, tmp_path, monkeypatch
) -> None:
    """The full-pipeline parity contract: persisting via
    ``build_and_persist_features`` with ``feature_source='sql'`` and then
    ``'lake'`` produces IDENTICAL persisted ``Feature`` rows for all 15
    lake-covered names — value, missing flag, source count, AND freshness —
    not just the in-memory values the direct parity test compares."""
    asset = _seed_sql_and_lake(session, tmp_path)
    session.commit()
    # build_and_persist_features builds the lake factory from the default
    # settings, so point them at the seeded lake directory.
    monkeypatch.setenv("ARCHIVE_BACKEND", "local")
    monkeypatch.setenv("ARCHIVE_LOCAL_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        build_and_persist_features(
            session,
            decision_ts=DECISION,
            asset_ids=[asset.id],
            feature_source="sql",
        )
        session.flush()
        all_sql = _persisted_feature_snapshot(session, asset.id, DECISION)
        sql_rows = {name: all_sql[name] for name in LAKE_FEATURE_NAMES}

        build_and_persist_features(
            session,
            decision_ts=DECISION,
            asset_ids=[asset.id],
            feature_source="lake",
        )
        session.flush()
        all_lake = _persisted_feature_snapshot(session, asset.id, DECISION)
        lake_rows = {name: all_lake[name] for name in LAKE_FEATURE_NAMES}

        assert set(sql_rows) == set(LAKE_FEATURE_NAMES)
        assert set(lake_rows) == set(LAKE_FEATURE_NAMES)
        for name in LAKE_FEATURE_NAMES:
            assert lake_rows[name] == sql_rows[name], (
                f"{name}: persisted row diverged (sql={sql_rows[name]} "
                f"lake={lake_rows[name]})"
            )
        # Sanity: the seeded asset's values are the known parity numbers.
        assert sql_rows["holder_count"] == (7.0, False, 1, 1.0)
        # 1 low_liquidity scan + 2 contract-analysis findings.
        assert sql_rows["suspicious_contract_flags"][0] == pytest.approx(3.0)
    finally:
        get_settings.cache_clear()


def test_lake_replay_fails_loudly_when_requested_asset_hour_is_missing(
    session, tmp_path, monkeypatch
) -> None:
    """Lake replay must not persist a synthetic incomplete snapshot when the
    requested asset-hour has no archived evidence."""
    asset = _seed_sql_and_lake(session, tmp_path)
    session.commit()
    monkeypatch.setenv("ARCHIVE_BACKEND", "local")
    monkeypatch.setenv("ARCHIVE_LOCAL_DIR", str(tmp_path / "missing"))
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="archived evidence.*asset-hour"):
            build_and_persist_features(
                session,
                decision_ts=DECISION,
                asset_ids=[asset.id],
                feature_source="lake",
            )
        assert session.scalar(
            select(func.count()).select_from(models.Feature).where(
                models.Feature.asset_id == asset.id,
                models.Feature.decision_ts == DECISION,
            )
        ) == 0
    finally:
        get_settings.cache_clear()


def test_lake_read_path_reports_missing_on_empty_lake(session, tmp_path) -> None:
    """No evidence in the lake -> the market block reports honest missing."""
    lake_values = LakeFeatureFactory(
        store=LocalArchiveStore(tmp_path), settings=_settings(tmp_path)
    ).build_for_asset(asset_address=BASE_ADDRESS, decision_ts=DECISION)
    assert set(lake_values) == set(LAKE_FEATURE_NAMES)
    assert all(
        value.missing
        for name, value in lake_values.items()
        if name != "suspicious_contract_flags"
    )
    # suspicious_contract_flags is reported as 0.0 (never missing), mirroring
    # the SQL path's contract-flag counter on an asset with no contracts.
    assert lake_values["suspicious_contract_flags"].value == 0.0
    assert not lake_values["suspicious_contract_flags"].missing


def test_lake_read_path_ignores_other_assets(session, tmp_path) -> None:
    """Only evidence for the requested base address enters the series."""
    _seed_sql_and_lake(session, tmp_path)
    session.commit()
    other = "TokenOther11111111111111111111111111111111"
    lake_values = LakeFeatureFactory(
        store=LocalArchiveStore(tmp_path), settings=_settings(tmp_path)
    ).build_for_asset(asset_address=other, decision_ts=DECISION)
    assert all(
        value.missing
        for name, value in lake_values.items()
        if name != "suspicious_contract_flags"
    )
    assert lake_values["suspicious_contract_flags"].value == 0.0


def test_lake_reconstructs_evm_holders_from_blockscout_evidence(
    session, tmp_path
) -> None:
    """The lake path reconstructs holder_count / holder_growth /
    top_holder_concentration for a Base/Ethereum asset from the archived
    ``evm_holders`` evidence (Blockscout) exactly as for Solana — the
    reconstruction is chain-agnostic, keyed on ``source_type='chain_rpc'``
    plus the mint/supply/largest_accounts payload shape."""
    chain = get_or_create_chain(
        session, "base", name="Base", vm_type="evm", native_symbol="ETH"
    )
    created_at = DECISION - timedelta(hours=6)
    evm_address = "0xTokenBase11111111111111111111111111111111"
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=evm_address,
        symbol="EVMT",
        name="EVM Token",
        first_seen_at=created_at,
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="0xWethBase111111111111111111111111111111111",
        symbol="WETH",
        name="Wrapped Ether",
        first_seen_at=created_at - timedelta(days=365),
    )
    upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="uniswap-v2-base",
        pair_address="0xPairBase111111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=created_at,
    )
    # EVM-shaped wallets carrying the same balances as the Solana arc, so the
    # expected numbers match (5 holders at -2h, 7 at the decision hour).
    evm_a = [
        (f"0x{letter * 40}", balance)
        for letter, balance in zip("abcde", [b for _, b in SNAPSHOT_A], strict=True)
    ]
    evm_b = evm_a + [
        ("0xf" * 40, 600.0),
        ("0xe" * 40, 700.0),
    ]
    _seed_holder_evidence(
        session,
        asset,
        with_sql_rows=True,
        source_name="evm_holders",
        snapshots=[(2, evm_a), (0, evm_b)],
    )
    session.flush()

    settings = _settings(tmp_path)
    RawEvidenceCompactor(
        store=LocalArchiveStore(tmp_path), settings=settings
    ).compact(session, DECISION + timedelta(minutes=30))
    session.flush()
    session.commit()

    sql_values = {
        value.name: value
        for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }
    lake_values = LakeFeatureFactory(
        store=LocalArchiveStore(tmp_path), settings=settings
    ).build_for_asset(asset_address=evm_address, decision_ts=DECISION)

    assert lake_values["holder_count"].value == pytest.approx(7.0)
    assert lake_values["holder_growth"].value == pytest.approx(2.0)
    assert lake_values["top_holder_concentration"].value == pytest.approx(0.28)
    for name in ("holder_count", "holder_growth", "top_holder_concentration"):
        assert lake_values[name].missing == sql_values[name].missing, f"{name} missing"
        assert lake_values[name].value == pytest.approx(
            sql_values[name].value, rel=1e-6
        ), f"{name}: sql={sql_values[name].value} lake={lake_values[name].value}"


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


def test_lake_reconstruction_cache_avoids_recompute(session, tmp_path, monkeypatch) -> None:
    """Repeated ``(asset, hour)`` builds hit the class-level cache instead of
    re-querying DuckDB, across factory instances (fresh factory per backtest
    step). Sub-hour decisions, other assets, and other hours bypass it, and
    ``clear_cache`` forces a fresh reconstruction."""
    asset = _seed_sql_and_lake(session, tmp_path)
    session.commit()
    store = LocalArchiveStore(tmp_path)
    settings = _settings(tmp_path)
    factory = LakeFeatureFactory(store=store, settings=settings)
    LakeFeatureFactory.clear_cache()

    calls = {"n": 0}
    original = factory._reconstruct_uncached

    def counting_reconstruct(asset_address, decision_ts):
        calls["n"] += 1
        return original(asset_address, decision_ts)

    monkeypatch.setattr(factory, "_reconstruct_uncached", counting_reconstruct)

    first = factory.build_for_asset(asset_address=asset.address, decision_ts=DECISION)
    second = factory.build_for_asset(asset_address=asset.address, decision_ts=DECISION)
    assert calls["n"] == 1
    assert first == second

    # A fresh factory (the backtest creates one per step) still hits the
    # class-level cache — no DuckDB re-query.
    fresh = LakeFeatureFactory(store=store, settings=settings)
    monkeypatch.setattr(fresh, "_reconstruct_uncached", counting_reconstruct)
    fresh.build_for_asset(asset_address=asset.address, decision_ts=DECISION)
    assert calls["n"] == 1

    # Different hour -> new key -> recompute.
    factory.build_for_asset(
        asset_address=asset.address, decision_ts=DECISION - timedelta(hours=1)
    )
    assert calls["n"] == 2
    # Different asset -> new key -> recompute.
    factory.build_for_asset(
        asset_address="TokenOther11111111111111111111111111111111", decision_ts=DECISION
    )
    assert calls["n"] == 3
    # Sub-hour decision -> bypasses the cache entirely (exactness guarantee).
    factory.build_for_asset(
        asset_address=asset.address, decision_ts=DECISION + timedelta(minutes=30)
    )
    assert calls["n"] == 4

    # Clearing the cache forces a fresh reconstruction.
    LakeFeatureFactory.clear_cache()
    factory.build_for_asset(asset_address=asset.address, decision_ts=DECISION)
    assert calls["n"] == 5

    LakeFeatureFactory.clear_cache()


def test_lake_batch_reconstructs_all_assets_in_one_download(session, tmp_path) -> None:
    """build_for_assets reconstructs every asset with ONE lake listing and ONE
    download per file (not per asset), matches the per-asset build for the
    seeded asset, and gives absent assets the empty/missing block — so a
    full-lake job like the parity CI is O(1) downloads, not O(assets)."""
    asset = _seed_lake_only(session, tmp_path)
    session.commit()
    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    LakeFeatureFactory.clear_cache()

    counters = {"list": 0, "download": 0}
    original_list = store.list_objects
    original_download = store.download_to

    def counting_list(*args, **kwargs):
        counters["list"] += 1
        return original_list(*args, **kwargs)

    def counting_download(*args, **kwargs):
        counters["download"] += 1
        return original_download(*args, **kwargs)

    store.list_objects = counting_list
    store.download_to = counting_download

    factory = LakeFeatureFactory(store=store, settings=settings)
    other = "TokenOther11111111111111111111111111111111"
    values = factory.build_for_assets([asset.address, other], DECISION)

    assert counters["list"] == 1
    expected_files = len(original_list(settings.archive_prefix))
    assert expected_files >= 1
    assert counters["download"] == expected_files
    # Per-asset results still match the single build for the seeded asset.
    single = factory.build_for_asset(asset_address=asset.address, decision_ts=DECISION)
    assert values[asset.address] == single
    # The absent asset gets the full lake-covered name set, empty/missing block
    # (suspicious_contract_flags is never missing, mirroring the SQL path).
    assert set(values[other]) == set(LAKE_FEATURE_NAMES)
    assert all(
        value.missing
        for name, value in values[other].items()
        if name != "suspicious_contract_flags"
    )

    LakeFeatureFactory.clear_cache()


def test_lake_cache_hit_miss_counters(session, tmp_path) -> None:
    """The class-level counters track cache hits vs computed reconstructions
    across both the single-asset and batched paths, and reset on demand."""
    asset = _seed_lake_only(session, tmp_path)
    session.commit()
    store = LocalArchiveStore(tmp_path)
    factory = LakeFeatureFactory(store=store, settings=_settings(tmp_path))
    LakeFeatureFactory.clear_cache()
    LakeFeatureFactory.reset_cache_stats()

    factory.build_for_asset(asset_address=asset.address, decision_ts=DECISION)
    stats = LakeFeatureFactory.cache_stats()
    assert stats == {"hits": 0, "misses": 1, "saved_queries": 0}

    factory.build_for_asset(asset_address=asset.address, decision_ts=DECISION)
    stats = LakeFeatureFactory.cache_stats()
    assert stats == {"hits": 1, "misses": 1, "saved_queries": 1}

    # Batch path counts per-asset: the seeded asset is a hit, the absent one a
    # miss (one DuckDB pass for both, but one reconstruction was saved).
    other = "TokenOther11111111111111111111111111111111"
    factory.build_for_assets([asset.address, other], DECISION)
    stats = LakeFeatureFactory.cache_stats()
    assert stats == {"hits": 2, "misses": 2, "saved_queries": 2}

    # Sub-hour decisions are never cached -> a computed miss.
    factory.build_for_asset(
        asset_address=asset.address, decision_ts=DECISION + timedelta(minutes=30)
    )
    stats = LakeFeatureFactory.cache_stats()
    assert stats == {"hits": 2, "misses": 3, "saved_queries": 2}

    LakeFeatureFactory.reset_cache_stats()
    assert LakeFeatureFactory.cache_stats() == {
        "hits": 0,
        "misses": 0,
        "saved_queries": 0,
    }
    LakeFeatureFactory.clear_cache()


def test_lake_backtest_reports_cache_savings(session, tmp_path, monkeypatch) -> None:
    """A lake replay backtest records lake_cache hits/misses/saved_queries in
    its run metrics: the first run over a window fills the cache (all misses),
    a second run over the same window serves everything from cache."""
    from backtest.runner import run_backtest

    _seed_lake_only(session, tmp_path)
    session.commit()
    # Point the default settings (used by build_and_persist_features and the
    # backtest runner) at the seeded lake directory.
    monkeypatch.setenv("ARCHIVE_BACKEND", "local")
    monkeypatch.setenv("ARCHIVE_LOCAL_DIR", str(tmp_path))
    get_settings.cache_clear()
    LakeFeatureFactory.clear_cache()
    LakeFeatureFactory.reset_cache_stats()
    try:
        start = DECISION - timedelta(hours=1)
        kwargs = dict(start=start, end=DECISION, top_k=5, forward_hours=1)
        first = run_backtest(session, feature_source="lake", **kwargs)
        session.commit()
        first_metrics = {
            row.metric_name: row.metric_value
            for row in session.scalars(
                select(models.BacktestResult).where(
                    models.BacktestResult.run_id == first.id
                )
            ).all()
        }
        assert first_metrics["lake_cache.misses"] >= 1
        assert first_metrics["lake_cache.hits"] == 0
        assert first_metrics["lake_cache.saved_queries"] == 0

        second = run_backtest(session, feature_source="lake", **kwargs)
        session.commit()
        second_metrics = {
            row.metric_name: row.metric_value
            for row in session.scalars(
                select(models.BacktestResult).where(
                    models.BacktestResult.run_id == second.id
                )
            ).all()
        }
        assert second_metrics["lake_cache.hits"] >= 1
        assert second_metrics["lake_cache.saved_queries"] >= 1
        assert second_metrics["lake_cache.misses"] == 0
    finally:
        get_settings.cache_clear()
        LakeFeatureFactory.clear_cache()


def test_lake_cache_consistent_under_concurrent_builds(session, tmp_path) -> None:
    """Concurrent threads building the same (asset, hour) simultaneously all
    get identical reconstructions, leave exactly one coherent cache entry per
    key, and keep the hit/miss accounting consistent (hits + misses == the
    number of lookups) — no torn state or lost updates under the lock."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    asset = _seed_lake_only(session, tmp_path)
    session.commit()
    store = LocalArchiveStore(tmp_path)
    factory = LakeFeatureFactory(store=store, settings=_settings(tmp_path))
    LakeFeatureFactory.clear_cache()
    LakeFeatureFactory.reset_cache_stats()

    baseline = factory.build_for_asset(asset_address=asset.address, decision_ts=DECISION)
    key = (factory._store_key(), asset.address, DECISION)

    def _run_concurrent(worker, n: int = 8) -> list[object]:
        barrier = threading.Barrier(n)

        def _wrapped(index: int):
            barrier.wait()  # maximize contention: all threads start together
            return worker(index)

        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(_wrapped, range(n)))

    # Single-asset path: 8 threads race on the same (asset, hour).
    LakeFeatureFactory.clear_cache()
    LakeFeatureFactory.reset_cache_stats()
    results = _run_concurrent(
        lambda _: factory.build_for_asset(
            asset_address=asset.address, decision_ts=DECISION
        )
    )
    for result in results:
        assert result == baseline
    with LakeFeatureFactory._cache_lock:
        entries = [cache_key for cache_key in LakeFeatureFactory._cache if cache_key == key]
    assert len(entries) == 1, "exactly one coherent cache entry for the key"
    stats = LakeFeatureFactory.cache_stats()
    assert stats["hits"] + stats["misses"] == len(results)

    # Batched path: 8 threads race on the same address set (one hit/miss per
    # address per thread), leaving one entry per asset.
    other = "TokenOther11111111111111111111111111111111"
    addresses = [asset.address, other]
    baseline_multi = factory.build_for_assets(addresses, DECISION)
    LakeFeatureFactory.clear_cache()
    LakeFeatureFactory.reset_cache_stats()
    results = _run_concurrent(lambda _: factory.build_for_assets(addresses, DECISION))
    for result in results:
        assert result == baseline_multi
    with LakeFeatureFactory._cache_lock:
        cached_keys = set(LakeFeatureFactory._cache)
    assert key in cached_keys
    assert (factory._store_key(), other, DECISION) in cached_keys
    stats = LakeFeatureFactory.cache_stats()
    assert stats["hits"] + stats["misses"] == len(results) * len(addresses)

    LakeFeatureFactory.clear_cache()
