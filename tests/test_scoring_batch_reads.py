from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from features.factory import FeatureFactory, _load_source_names
from scoring.engine import ScoringEngine
from storage import models
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    store_raw_evidence,
    upsert_asset,
    upsert_pool_and_pair,
    upsert_social_mention,
)
from tests.conftest import seed_market_asset, seed_reference


def _features_by_name(session, asset, decision_ts, *, source_names=None) -> dict[str, object]:
    values = FeatureFactory().build_for_asset(
        session,
        asset,
        decision_ts,
        source_names=source_names,
    )
    return {value.name: value for value in values}


def test_load_source_names_is_one_batched_map(session) -> None:
    """The source id→name map loads once per scan, not per asset."""
    get_or_create_source(session, name="youtube_rss", source_type="social", tier="x")
    get_or_create_source(session, name="telegram", source_type="social", tier="x")
    session.flush()

    names = _load_source_names(session)
    assert set(names.values()) == {"youtube_rss", "telegram"}


def test_velocity_features_identical_with_explicit_source_map(session) -> None:
    """Supplying the scan-level source map must not change feature values."""
    asset = seed_market_asset(session)
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    youtube = get_or_create_source(
        session, name="youtube_rss", source_type="social", tier="public_metadata"
    )
    github = get_or_create_source(
        session, name="github_public", source_type="public_metadata", tier="public_metadata"
    )
    now_minus_1h = decision_ts - timedelta(hours=1)
    repo_url = "https://github.com/example/hype"
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
        payload={"items": [{"url": repo_url, "title": "example/hype", "metrics": {"stars": 100}}]},
        observed_at=decision_ts - timedelta(hours=36),
    )
    store_raw_evidence(
        session,
        source=github,
        payload={"items": [{"url": repo_url, "title": "example/hype", "metrics": {"stars": 130}}]},
        observed_at=decision_ts,
    )
    session.commit()

    with_map = _features_by_name(
        session, asset, decision_ts, source_names=_load_source_names(session)
    )
    default = _features_by_name(session, asset, decision_ts)

    for name in ("kol_velocity", "github_star_velocity", "hf_download_velocity"):
        assert with_map[name].value == default[name].value
        assert with_map[name].missing == default[name].missing
    assert with_map["kol_velocity"].value == 1.0
    assert with_map["github_star_velocity"].value == 20.0


def test_persist_for_assets_shares_source_map_across_assets(session) -> None:
    """persist_for_assets computes all assets without per-asset source reloads.

    The behavioural contract is that every asset's velocity features persist
    identically to a per-asset build; the invariant (source map) is loaded
    once and shared.
    """
    asset_a = seed_market_asset(
        session, symbol="ALPHA", address="Alpha11111111111111111111111111111111111111"
    )
    asset_b = seed_market_asset(
        session,
        symbol="BETA",
        address="Beta111111111111111111111111111111111111111",
        pair_address="Pair222222222222222222222222222222222222",
    )
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    youtube = get_or_create_source(
        session, name="youtube_rss", source_type="social", tier="public_metadata"
    )
    for asset in (asset_a, asset_b):
        upsert_social_mention(
            session,
            asset_id=asset.id,
            topic=f"{asset.symbol} video",
            source_id=youtube.id,
            ts=decision_ts - timedelta(hours=1),
            observed_at=decision_ts - timedelta(hours=1),
            metrics_json={"channel_id": f"UC{asset.symbol}"},
            raw_ref=f"https://youtube.com/watch?v={asset.symbol}",
        )
    session.commit()

    factory = FeatureFactory()
    # Scope to the two base assets (the fixtures also seed a quote asset each,
    # which must not leak into the scan's results).
    output = factory.persist_for_assets(
        session, decision_ts=decision_ts, asset_ids=[asset_a.id, asset_b.id]
    )
    assert set(output) == {asset_a.id, asset_b.id}
    for asset in (asset_a, asset_b):
        persisted = session.scalar(
            select(models.Feature).where(
                models.Feature.asset_id == asset.id,
                models.Feature.decision_ts == decision_ts,
                models.Feature.feature_name == "kol_velocity",
            )
        )
        assert persisted is not None
        assert persisted.feature_value == 1.0
        assert persisted.missing_flag is False


def test_build_price_updates_batches_across_assets_and_chains(session) -> None:
    """_build_price_updates returns correct rows for many assets and chains."""
    _solana, dexscreener = seed_reference(session)
    ether = get_or_create_chain(
        session, "ethereum", name="Ethereum", vm_type="evm", native_symbol="ETH"
    )
    asset_sol = seed_market_asset(
        session,
        symbol="SOLT",
        address="SolA11111111111111111111111111111111111111",
        pair_address="PairSol11111111111111111111111111111111111",
    )
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    asset_eth = upsert_asset(
        session,
        chain_id=ether.id,
        address="0xEthToke" + "0" * 30,
        symbol="ETHT",
        name="Ethereum Fixture",
        first_seen_at=now - timedelta(hours=2),
    )
    quote = upsert_asset(
        session,
        chain_id=ether.id,
        address="0x" + "1" * 40,
        symbol="WETH",
        name="Wrapped Ether",
        first_seen_at=now - timedelta(days=365),
    )
    pool, pair = upsert_pool_and_pair(
        session,
        chain_id=ether.id,
        dex_id="uniswap",
        pair_address="0xPair" + "2" * 40,
        base_asset_id=asset_eth.id,
        quote_asset_id=quote.id,
        created_at_source=now - timedelta(hours=2),
    )
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=dexscreener.id,
        ts=now,
        observed_at=now,
        price_usd=9.5,
        volume_usd=42_000,
        buys=30,
        sells=3,
        trades=33,
    )
    insert_liquidity_snapshot_once(
        session,
        pool_id=pool.id,
        source_id=dexscreener.id,
        ts=now,
        observed_at=now,
        reserve_usd=250_000,
    )
    session.commit()

    updates = ScoringEngine()._build_price_updates(
        session, [asset_sol.id, asset_eth.id], now.isoformat()
    )
    by_id = {update.asset_id: update for update in updates}
    assert set(by_id) == {asset_sol.id, asset_eth.id}

    sol_update = by_id[asset_sol.id]
    assert sol_update.symbol == "SOLT"
    assert sol_update.chain == "solana"
    assert sol_update.price_usd is not None
    assert sol_update.liquidity_usd is not None

    eth_update = by_id[asset_eth.id]
    assert eth_update.symbol == "ETHT"
    assert eth_update.chain == "ethereum"
    assert eth_update.price_usd == 9.5
    assert eth_update.volume_usd == 42_000
    assert eth_update.liquidity_usd == 250_000
