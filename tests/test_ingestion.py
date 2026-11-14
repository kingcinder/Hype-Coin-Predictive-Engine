from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

import ingestion.service as ingestion_service
from common.config import get_settings
from common.time import ensure_utc
from ingestion.contract_analyzer import ContractAnalysis
from ingestion.normalizers import NormalizedPair
from ingestion.provider_gate import CircuitOpen
from ingestion.service import IngestionService, _analysis_findings
from ingestion.solana_swaps import (
    fetch_pool_swaps,
    reset_solana_swap_cache,
    reset_solana_swap_gate,
)
from ingestion.source_clients import GeckoTerminalClient, reset_geckoterminal_gate
from ops.archive import LocalArchiveStore
from storage import models
from storage.repository import (
    get_or_create_chain,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_market_asset, seed_reference


@pytest.fixture(autouse=True)
def _reset_provider_circuits():
    reset_geckoterminal_gate()
    reset_solana_swap_gate()
    reset_solana_swap_cache()
    yield
    reset_geckoterminal_gate()
    reset_solana_swap_gate()
    reset_solana_swap_cache()


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


def test_pair_ingest_commits_prior_pairs_before_pool_trade_fetch(session, monkeypatch) -> None:
    """Reusing one session for several pairs must release the write lock first.

    The second pair's GeckoTerminal fetch must see the first pair committed and
    must run without this session holding SQLite's write lock. A separate
    connection proves both by taking BEGIN IMMEDIATE during the fetch.
    """
    seed_reference(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None
    session.commit()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    monkeypatch.setattr(get_settings(), "first_buy_trade_fetch_enabled", True)

    observed: list[tuple[str, int]] = []

    def fake_pool_trades(self, chain: str, pool: str):
        raw = session.get_bind().raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA busy_timeout=100")
            cursor.execute("BEGIN IMMEDIATE")
            count = cursor.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
            raw.rollback()
        finally:
            raw.close()
        observed.append((pool, count))
        return []

    monkeypatch.setattr(ingestion_service.GeckoTerminalClient, "pool_trades", fake_pool_trades)
    service = IngestionService()

    def payload(pair_address: str, token_address: str) -> dict[str, object]:
        return {
            "chainId": "solana",
            "dexId": "raydium",
            "pairAddress": pair_address,
            "pairCreatedAt": int(now.timestamp() * 1000),
            "baseToken": {
                "address": token_address,
                "symbol": f"T{token_address[-1]}",
                "name": "Same Session Token",
            },
            "quoteToken": {"address": "USDC", "symbol": "USDC", "name": "USD Coin"},
            "liquidity": {"usd": 80000},
        }

    first_pair = "PairSameSession1111111111111111111111111111"
    second_pair = "PairSameSession2222222222222222222222222222"
    assert service._store_dexscreener_pair(
        session, source, payload(first_pair, "TokenSameSession1111111111111111111111111111")
    )
    # Deliberately no commit here: the next pair's fetch must commit this one.
    assert service._store_dexscreener_pair(
        session, source, payload(second_pair, "TokenSameSession2222222222222222222222222222")
    )

    assert observed == [(first_pair, 0), (second_pair, 1)]


def test_geckoterminal_pool_trades_reads_recent_wallet_trades(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "geckoterminal_min_interval_seconds", 0)
    client = GeckoTerminalClient()
    requested: list[str] = []

    def fake_get_json(path: str):
        requested.append(path)
        return {"data": [{"id": "trade-1", "type": "trade"}]}

    client.http.get_json = fake_get_json  # type: ignore[method-assign]
    try:
        assert client.pool_trades("solana", "PoolABC") == [{"id": "trade-1", "type": "trade"}]
    finally:
        client.close()
    assert requested == ["/api/v2/networks/solana/pools/PoolABC/trades"]


def test_geckoterminal_429_opens_circuit(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "geckoterminal_min_interval_seconds", 0)
    monkeypatch.setattr(get_settings(), "provider_circuit_break_seconds", 3600)
    client = GeckoTerminalClient()
    calls: list[str] = []

    def rate_limited(path: str):
        calls.append(path)
        request = httpx.Request("GET", f"https://api.geckoterminal.com{path}")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("429", request=request, response=response)

    client.http.get_json = rate_limited  # type: ignore[method-assign]
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.pool_trades("solana", "Pool429")
        with pytest.raises(CircuitOpen):
            client.pool_trades("solana", "Pool429")
    finally:
        client.close()
    assert len(calls) == 1


def test_solana_rpc_429_opens_circuit(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "solana_swap_rpc_min_interval_seconds", 0)
    monkeypatch.setattr(get_settings(), "provider_circuit_break_seconds", 3600)
    calls: list[str] = []

    class RateLimitedRpc:
        def get_signatures_for_address(self, address: str, *, limit: int):
            calls.append(address)
            request = httpx.Request("POST", "https://solana-rpc.publicnode.com")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("429", request=request, response=response)

    assert fetch_pool_swaps(RateLimitedRpc(), pool_address="Pool429", base_mint="Mint") == []
    assert fetch_pool_swaps(RateLimitedRpc(), pool_address="Pool429", base_mint="Mint") == []
    assert calls == ["Pool429"]


def test_solana_swap_cache_budgets_transactions_without_losing_buys(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "solana_swap_rpc_min_interval_seconds", 0)
    monkeypatch.setattr(get_settings(), "first_buy_swap_cache_seconds", 300)
    transactions: list[str] = []

    class Rpc:
        def get_signatures_for_address(self, address: str, *, limit: int):
            return [{"signature": f"sig-{i}", "err": None} for i in range(5, 0, -1)]

        def get_transaction(self, signature: str):
            transactions.append(signature)
            wallet = signature.replace("sig", "W")
            return {
                "result": {
                    "blockTime": 1_777_000_000,
                    "meta": {
                        "preTokenBalances": [
                            {"mint": "Mint", "owner": wallet, "uiTokenAmount": {"uiAmount": 0}}
                        ],
                        "postTokenBalances": [
                            {"mint": "Mint", "owner": wallet, "uiTokenAmount": {"uiAmount": 1}}
                        ],
                    },
                }
            }

    rpc = Rpc()
    first = fetch_pool_swaps(rpc, pool_address="Pool", base_mint="Mint", limit=2)
    second = fetch_pool_swaps(rpc, pool_address="Pool", base_mint="Mint", limit=2)
    third = fetch_pool_swaps(rpc, pool_address="Pool", base_mint="Mint", limit=2)
    fourth = fetch_pool_swaps(rpc, pool_address="Pool", base_mint="Mint", limit=2)

    assert [len(first), len(second), len(third), len(fourth)] == [2, 4, 5, 5]
    assert len(transactions) == 5
    assert set(transactions) == {f"sig-{i}" for i in range(1, 6)}


def test_successful_empty_geckoterminal_feed_skips_solana_rpc_fallback(monkeypatch) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    pair = NormalizedPair(
        chain_slug="solana",
        dex_id="raydium",
        pair_address="PoolEmptyFeed",
        base_address="MintEmptyFeed",
        base_symbol="EMPTY",
        base_name="Empty Feed Token",
        quote_address="USDC",
        quote_symbol="USDC",
        quote_name="USD Coin",
        pair_created_at=now,
        price_usd=1.0,
        volume_usd=0.0,
        liquidity_usd=80000.0,
        reserve_base=None,
        reserve_quote=None,
        buys=0,
        sells=0,
        trades=0,
    )
    monkeypatch.setattr(
        ingestion_service.GeckoTerminalClient,
        "pool_trades",
        lambda self, chain, pool: [],
    )

    class UnexpectedRpc:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("RPC fallback ran for a successful empty feed")

    monkeypatch.setattr(ingestion_service, "SolanaRpcClient", UnexpectedRpc)
    service = IngestionService()

    assert service._first_buy_swaps(pair, now, set()) == ([], True)
    assert service._first_buy_stats["gecko_failures"] == 0
    assert service._first_buy_stats["rpc_fallback_hits"] == 0


def test_successful_empty_geckoterminal_feed_persists_zero_stats(session, monkeypatch) -> None:
    """An empty-but-successful feed is observed data: zero buys/sells, not unknown.

    The pool-trades call must succeed and the RPC fallback must never run, yet
    the first-window stats are written as 0/0 instead of NULL.
    """
    seed_reference(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    monkeypatch.setattr(
        ingestion_service.GeckoTerminalClient,
        "pool_trades",
        lambda self, chain, pool: [],
    )

    class UnexpectedRpc:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("RPC fallback ran for a successful empty feed")

    monkeypatch.setattr(ingestion_service, "SolanaRpcClient", UnexpectedRpc)
    pool_address = "PairEmptyFeedStats1111111111111111111111111"
    mint = "TokenEmptyFeedStats111111111111111111111111"
    payload = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": pool_address,
        "pairCreatedAt": int(now.timestamp() * 1000),
        "baseToken": {"address": mint, "symbol": "EMPTY", "name": "Empty Feed Token"},
        "quoteToken": {"address": "USDC", "symbol": "USDC", "name": "USD Coin"},
        "liquidity": {"usd": 80000},
    }

    service = IngestionService()
    assert service._store_dexscreener_pair(session, source, payload) is True
    session.commit()

    assert service._first_buy_stats["gecko_failures"] == 0
    assert service._first_buy_stats["rpc_fallback_hits"] == 0
    asset = session.scalar(select(models.Asset).where(models.Asset.address == mint))
    assert asset is not None
    stats = session.scalar(
        select(models.PairFirstWindowStats).where(models.PairFirstWindowStats.asset_id == asset.id)
    )
    assert stats is not None
    assert stats.buys == 0
    assert stats.sells == 0
    assert stats.buy_sell_ratio is None


def test_ingestion_gecko_failure_without_trades_keeps_stats_unknown(session, monkeypatch) -> None:
    seed_reference(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    settings = get_settings()
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    monkeypatch.setattr(settings, "copy_trading_enabled", True)
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=now, kind="armed", reason="test"))
    session.commit()

    def wallets() -> list[dict[str, str]]:
        return [{"address": "W", "chain": "solana"}]

    monkeypatch.setattr("trading.wallets.load_wallets", wallets)
    monkeypatch.setattr("trading.copy.load_wallets", wallets)

    def rate_limited(self, chain: str, pool: str):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(ingestion_service.GeckoTerminalClient, "pool_trades", rate_limited)
    rpc_calls: list[str] = []

    def empty_rpc_fallback(*args, **kwargs):
        rpc_calls.append(kwargs["pool_address"])
        return []

    monkeypatch.setattr(ingestion_service, "fetch_pool_swaps", empty_rpc_fallback)
    pair_address = "PairUnknownStats11111111111111111111111111"
    mint = "TokenUnknownStats11111111111111111111111111"
    payload = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": pair_address,
        "pairCreatedAt": int(now.timestamp() * 1000),
        "baseToken": {"address": mint, "symbol": "UNK", "name": "Unknown Stats Token"},
        "quoteToken": {"address": "USDC", "symbol": "USDC", "name": "USD Coin"},
        "liquidity": {"usd": 80000},
    }

    service = IngestionService()
    assert service._store_dexscreener_pair(session, source, payload) is True
    session.commit()

    assert rpc_calls == [pair_address]
    pool = session.scalar(select(models.Pool).where(models.Pool.address == pair_address))
    assert pool is not None
    pair = session.scalar(select(models.Pair).where(models.Pair.pool_id == pool.id))
    assert pair is not None
    asset = session.scalar(select(models.Asset).where(models.Asset.address == mint))
    assert asset is not None
    stats = session.scalar(
        select(models.PairFirstWindowStats).where(models.PairFirstWindowStats.asset_id == asset.id)
    )
    assert stats is not None
    assert stats.buys is None
    assert stats.sells is None
    assert stats.buy_sell_ratio is None
    assert session.scalars(select(models.CopyLog)).all() == []
    assert session.scalars(select(models.LedgerOpenPosition)).all() == []


def test_first_buy_health_warns_when_budget_skips_fetches(session) -> None:
    service = IngestionService()
    service._first_buy_stats["pools"] = 8
    service._first_buy_stats["budget_skipped"] = 12

    service._record_first_buy_health(session)
    session.commit()

    health = session.scalars(
        select(models.SystemHealth).where(models.SystemHealth.component == "source:first_buy_feed")
    ).one()
    assert health.state == "yellow"
    assert "budget_skipped=12" in health.message
    assert "gecko_failures=0" in health.message


def test_first_buy_trade_budget_caps_gecko_calls_per_scan(monkeypatch) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(get_settings(), "first_buy_trade_max_pools_per_scan", 2)
    calls: list[str] = []
    monkeypatch.setattr(
        ingestion_service.GeckoTerminalClient,
        "pool_trades",
        lambda self, chain, pool: calls.append(pool) or [],
    )

    def pair(address: str) -> NormalizedPair:
        return NormalizedPair(
            chain_slug="solana",
            dex_id="raydium",
            pair_address=address,
            base_address="MintBudget",
            base_symbol="BUDGET",
            base_name="Budget Token",
            quote_address="USDC",
            quote_symbol="USDC",
            quote_name="USD Coin",
            pair_created_at=now,
            price_usd=1.0,
            volume_usd=0.0,
            liquidity_usd=80000.0,
            reserve_base=None,
            reserve_quote=None,
            buys=0,
            sells=0,
            trades=0,
        )

    service = IngestionService()
    for index in range(3):
        # The third pool is skipped by the budget, so its feed was never read.
        assert service._first_buy_swaps(pair(f"PoolBudget{index}"), now, set()) == (
            [],
            index < 2,
        )

    assert calls == ["PoolBudget0", "PoolBudget1"]
    assert service._first_buy_stats["pools"] == 2
    assert service._first_buy_stats["budget_skipped"] == 1

    service._reset_first_buy_stats()
    assert service._first_buy_swaps(pair("PoolBudgetReset"), now, set()) == ([], True)
    assert calls == ["PoolBudget0", "PoolBudget1", "PoolBudgetReset"]


def test_first_buy_trade_rate_limit_backs_off_for_rest_of_scan(monkeypatch) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(get_settings(), "first_buy_trade_max_pools_per_scan", 8)
    calls: list[str] = []
    fallback_calls: list[str] = []

    def rate_limited(self, chain: str, pool: str):
        calls.append(pool)
        request = httpx.Request("GET", f"https://api.geckoterminal.com/{pool}")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("429", request=request, response=response)

    monkeypatch.setattr(ingestion_service.GeckoTerminalClient, "pool_trades", rate_limited)
    monkeypatch.setattr(
        ingestion_service,
        "fetch_pool_swaps",
        lambda *args, **kwargs: fallback_calls.append(kwargs["pool_address"]) or [],
    )

    def pair(address: str) -> NormalizedPair:
        return NormalizedPair(
            chain_slug="solana",
            dex_id="raydium",
            pair_address=address,
            base_address="MintRateLimit",
            base_symbol="LIMIT",
            base_name="Rate Limit Token",
            quote_address="USDC",
            quote_symbol="USDC",
            quote_name="USD Coin",
            pair_created_at=now,
            price_usd=1.0,
            volume_usd=0.0,
            liquidity_usd=80000.0,
            reserve_base=None,
            reserve_quote=None,
            buys=0,
            sells=0,
            trades=0,
        )

    service = IngestionService()
    assert service._first_buy_swaps(pair("PoolRateLimit0"), now, set()) == ([], False)
    assert service._first_buy_swaps(pair("PoolRateLimit1"), now, set()) == ([], False)

    assert calls == ["PoolRateLimit0"]
    assert fallback_calls == ["PoolRateLimit0"]
    assert service._first_buy_stats["gecko_failures"] == 1
    assert service._first_buy_stats["backoff_skipped"] == 1


def test_first_buy_rate_limit_backoff_resets_on_next_scan(session, monkeypatch) -> None:
    """The 429 backoff is per scan: the next scan fetches that pool again.

    Clearing only the per-scan pool budget would leave ``_first_buy_provider_backoff``
    set forever. A stubbed ``run_once`` (the real scan boundary) must reset it.
    """
    seed_reference(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    monkeypatch.setattr(get_settings(), "first_buy_trade_max_pools_per_scan", 8)

    def pair(address: str) -> NormalizedPair:
        return NormalizedPair(
            chain_slug="solana",
            dex_id="raydium",
            pair_address=address,
            base_address="MintBackoffReset",
            base_symbol="RESET",
            base_name="Backoff Reset Token",
            quote_address="USDC",
            quote_symbol="USDC",
            quote_name="USD Coin",
            pair_created_at=now,
            price_usd=1.0,
            volume_usd=0.0,
            liquidity_usd=80000.0,
            reserve_base=None,
            reserve_quote=None,
            buys=0,
            sells=0,
            trades=0,
        )

    def rate_limited(self, chain: str, pool: str):
        request = httpx.Request("GET", f"https://api.geckoterminal.com/{pool}")
        raise httpx.HTTPStatusError(
            "429", request=request, response=httpx.Response(429, request=request)
        )

    monkeypatch.setattr(ingestion_service.GeckoTerminalClient, "pool_trades", rate_limited)
    monkeypatch.setattr(
        ingestion_service,
        "fetch_pool_swaps",
        lambda *args, **kwargs: [],
    )
    service = IngestionService()
    assert service._first_buy_swaps(pair("PoolBackoff0"), now, set()) == ([], False)
    assert service._first_buy_provider_backoff is True

    # Stub every scan stage so run_once exercises the real scan boundary only.
    monkeypatch.setattr(service, "_ingest_dexscreener_profiles", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_dexscreener_boosts", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_geckoterminal_new_pools", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_birdeye_solana", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_solana_holder_snapshots", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_solana_safety_snapshots", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_evm_holder_snapshots", lambda session: 0)
    monkeypatch.setattr(service, "_run_data_quality_check", lambda session, decision_ts: {})
    monkeypatch.setattr(service, "_run_contract_analysis", lambda session, decision_ts: {})
    monkeypatch.setattr(ingestion_service, "run_mempool", lambda session, decision_ts=None: {})
    monkeypatch.setattr(ingestion_service, "run_narrative", lambda session, decision_ts=None: {})
    monkeypatch.setattr(ingestion_service, "extract_catalysts", lambda session, decision_ts=None: 0)
    monkeypatch.setattr(
        ingestion_service, "alert_upcoming_catalysts", lambda session, decision_ts=None: 0
    )
    monkeypatch.setattr(
        ingestion_service, "run_lifecycle", lambda session, decision_ts=None: {"events": 0}
    )
    monkeypatch.setattr(
        ingestion_service, "run_forecast_if_due", lambda session, decision_ts=None: {"forecasts": 0}
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

    service.run_once(session)

    assert service._first_buy_provider_backoff is False
    calls: list[str] = []
    monkeypatch.setattr(
        ingestion_service.GeckoTerminalClient,
        "pool_trades",
        lambda self, chain, pool: calls.append(pool) or [],
    )
    assert service._first_buy_swaps(pair("PoolBackoff1"), now, set()) == ([], True)
    assert calls == ["PoolBackoff1"]


def test_first_buy_ingestion_uses_real_pool_trades(session, monkeypatch) -> None:
    seed_reference(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    monkeypatch.setattr(get_settings(), "copy_trading_enabled", True)

    def wallets() -> list[dict[str, str]]:
        return [{"address": "W", "chain": "solana"}]

    monkeypatch.setattr("trading.wallets.load_wallets", wallets)
    monkeypatch.setattr("trading.copy.load_wallets", wallets)
    trade = {
        "attributes": {
            "kind": "buy",
            "tx_from_address": "W",
            "block_timestamp": (now + timedelta(seconds=30)).isoformat(),
            "volume_in_usd": "25",
        }
    }
    monkeypatch.setattr(
        ingestion_service.GeckoTerminalClient,
        "pool_trades",
        lambda self, chain, pool: [trade],
    )
    payload = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "PairTracked111111111111111111111111111111",
        "pairCreatedAt": int(now.timestamp() * 1000),
        "baseToken": {
            "address": "TokenTracked1111111111111111111111111111",
            "symbol": "TRACK",
            "name": "Tracked Token",
        },
        "quoteToken": {"address": "USDC", "symbol": "USDC", "name": "USD Coin"},
        "liquidity": {"usd": 80000},
    }

    service = IngestionService()
    assert service._store_dexscreener_pair(session, source, payload) is True
    session.commit()

    asset = session.scalar(
        select(models.Asset).where(models.Asset.address == payload["baseToken"]["address"])
    )
    assert asset is not None
    first_buy = session.scalar(select(models.FirstBuy).where(models.FirstBuy.asset_id == asset.id))
    assert first_buy is not None
    assert first_buy.wallet == "W"
    assert first_buy.is_tracked_wallet is True
    stats = session.scalar(
        select(models.PairFirstWindowStats).where(models.PairFirstWindowStats.asset_id == asset.id)
    )
    assert stats is not None and stats.buys == 1 and stats.sells == 0
    log = session.scalar(select(models.CopyLog))
    assert log is not None and log.status == "paper_only"
    assert session.scalars(select(models.LedgerOpenPosition)).all() == []


def test_tracked_first_buy_ingestion_queues_pending_copy_only(session, monkeypatch) -> None:
    seed_reference(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    settings = get_settings()
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    monkeypatch.setattr(settings, "copy_trading_enabled", True)
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=now, kind="armed", reason="test"))
    session.commit()

    def wallets() -> list[dict[str, str]]:
        return [{"address": "W", "chain": "solana"}]

    monkeypatch.setattr("trading.wallets.load_wallets", wallets)
    monkeypatch.setattr("trading.copy.load_wallets", wallets)
    feed_calls: list[tuple[str, str]] = []

    def pool_trades(self, chain: str, pool: str) -> list[dict]:
        feed_calls.append((chain, pool))
        return [
            {
                "attributes": {
                    "kind": "buy",
                    "tx_from_address": "W",
                    "block_timestamp": (now + timedelta(seconds=30)).isoformat(),
                    "volume_in_usd": "25",
                }
            }
        ]

    monkeypatch.setattr(ingestion_service.GeckoTerminalClient, "pool_trades", pool_trades)
    pair_address = "PairLiveCopy1111111111111111111111111111111"
    mint = "TokenLiveCopy111111111111111111111111111111"
    payload = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": pair_address,
        "pairCreatedAt": int(now.timestamp() * 1000),
        "baseToken": {"address": mint, "symbol": "LIVE", "name": "Live Copy Token"},
        "quoteToken": {"address": "USDC", "symbol": "USDC", "name": "USD Coin"},
        "liquidity": {"usd": 80000},
    }

    service = IngestionService()
    assert service._store_dexscreener_pair(session, source, payload) is True
    session.commit()

    assert feed_calls == [("solana", pair_address)]
    log = session.scalars(select(models.CopyLog)).one()
    assert log.status == "pending"
    assert log.wallet == "W"
    assert log.mint == mint
    assert log.detail is not None and log.detail.startswith("awaiting human approval")
    assert session.scalars(select(models.LedgerOpenPosition)).all() == []
    assert session.scalars(select(models.LiveFill)).all() == []


def test_ingestion_rpc_fallback_queues_pending_copy_and_records_health(
    session, monkeypatch
) -> None:
    seed_reference(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    settings = get_settings()
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    monkeypatch.setattr(settings, "copy_trading_enabled", True)
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=now, kind="armed", reason="test"))
    session.commit()

    def wallets() -> list[dict[str, str]]:
        return [{"address": "W", "chain": "solana"}]

    monkeypatch.setattr("trading.wallets.load_wallets", wallets)
    monkeypatch.setattr("trading.copy.load_wallets", wallets)

    def rate_limited(self, chain: str, pool: str):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(ingestion_service.GeckoTerminalClient, "pool_trades", rate_limited)

    pair_address = "PairRpcFallback11111111111111111111111111111"
    mint = "TokenRpcFallback1111111111111111111111111111"
    transaction = {
        "result": {
            "blockTime": int((now + timedelta(seconds=30)).timestamp()),
            "meta": {
                "preTokenBalances": [
                    {"mint": mint, "owner": "W", "uiTokenAmount": {"uiAmount": 0}}
                ],
                "postTokenBalances": [
                    {"mint": mint, "owner": "W", "uiTokenAmount": {"uiAmountString": "100"}}
                ],
            },
        }
    }
    requested_pools: list[str] = []

    class FakeRpc:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_signatures_for_address(self, address: str, *, limit: int):
            requested_pools.append(address)
            return [{"signature": "sig-1", "err": None}]

        def get_transaction(self, signature: str):
            assert signature == "sig-1"
            return transaction

        def close(self) -> None:
            pass

    monkeypatch.setattr(ingestion_service, "SolanaRpcClient", FakeRpc)
    payload = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": pair_address,
        "pairCreatedAt": int(now.timestamp() * 1000),
        "baseToken": {"address": mint, "symbol": "RPC", "name": "RPC Token"},
        "quoteToken": {"address": "USDC", "symbol": "USDC", "name": "USD Coin"},
        "priceUsd": "2.0",
        "liquidity": {"usd": 80000},
    }

    service = IngestionService()
    assert service._store_dexscreener_pair(session, source, payload) is True
    service._record_first_buy_health(session)
    session.commit()

    assert requested_pools == [pair_address]
    log = session.scalars(select(models.CopyLog)).one()
    assert log.status == "pending"
    assert log.wallet == "W"
    assert log.mint == mint
    assert session.scalars(select(models.LedgerOpenPosition)).all() == []
    assert session.scalars(select(models.LiveFill)).all() == []

    health = session.scalars(
        select(models.SystemHealth).where(models.SystemHealth.component == "source:first_buy_feed")
    ).one()
    assert health.state == "yellow"
    assert health.error_count == 1
    assert "gecko_failures=1" in health.message
    assert "rpc_fallback_hits=1" in health.message
    assert "rpc_fallback_swaps=1" in health.message
    assert "tracked_buys=1" in health.message
    assert "pending_copy_logs=1" in health.message


def test_geckoterminal_pool_ingest_creates_point_in_time_rows(session, monkeypatch) -> None:
    service = IngestionService()
    service.ensure_reference_data(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "geckoterminal"))
    assert source is not None
    observed_at = datetime(2026, 5, 1, 12, 15, 30, tzinfo=UTC)
    later_observation = observed_at + timedelta(minutes=1)
    observations = iter((observed_at, later_observation))
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: next(observations))
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
    pair = session.scalar(select(models.Pair))
    pool = session.scalar(select(models.Pool))
    contract = session.scalar(select(models.Contract))
    snapshot = session.scalar(select(models.MarketSnapshot))
    assert pair is not None and ensure_utc(pair.created_at) == observed_at
    assert pool is not None and ensure_utc(pool.created_at) == observed_at
    assert contract is not None and ensure_utc(contract.created_at) == observed_at
    assert snapshot is not None and ensure_utc(snapshot.observed_at) == observed_at


def test_solana_holder_snapshot_ingest_is_bounded_and_idempotent(session, monkeypatch) -> None:
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

    contract = session.scalar(select(models.Contract).where(models.Contract.asset_id == asset.id))
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


def test_evm_holder_snapshot_ingest_is_bounded_and_idempotent(session, monkeypatch) -> None:
    """The EVM holder scan (Blockscout) stores the same evidence shape as the
    Solana path on the evm_holders chain_rpc source, writes Holder rows with
    pct-of-supply, reports source:evm_holders health, and is idempotent on a
    repeat scan."""
    chain = get_or_create_chain(session, "base", name="Base", vm_type="evm", native_symbol="ETH")
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

    holders = session.scalars(select(models.Holder).where(models.Holder.asset_id == asset.id)).all()
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


def test_birdeye_without_api_key_skips_feed_and_reports_configuration(session, monkeypatch) -> None:
    """No key is a configuration state: skip the request, say so, stay yellow."""
    seed_reference(session)
    monkeypatch.setattr(get_settings(), "birdeye_api_key", None)

    class UnexpectedClient:
        BASE_URL = "https://public-api.birdeye.so"

        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("Birdeye was called without BIRDEYE_API_KEY")

    monkeypatch.setattr(ingestion_service, "BirdeyeClient", UnexpectedClient)
    service = IngestionService()

    assert service._ingest_birdeye_solana(session) == 0

    health = session.scalars(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "source:birdeye")
        .order_by(models.SystemHealth.ts.desc())
    ).first()
    assert health is not None
    assert health.state == "yellow"
    assert "BIRDEYE_API_KEY" in (health.message or "")
    assert "not set" in (health.message or "")
    assert "skipped" in (health.message or "")
    assert health.error_count == 0


def test_birdeye_rejected_key_is_reported_as_configuration_error(session, monkeypatch) -> None:
    """A set-but-rejected key must read as misconfiguration, not an outage."""
    seed_reference(session)
    monkeypatch.setattr(get_settings(), "birdeye_api_key", "test-birdeye-key")
    request = httpx.Request("GET", "https://public-api.birdeye.so/defi/v3/token/new_listing")
    response = httpx.Response(401, request=request)

    class RejectedKeyClient:
        BASE_URL = "https://public-api.birdeye.so"

        def __init__(self, api_key: str | None = None) -> None:
            assert api_key == "test-birdeye-key"

        def new_tokens(self, limit: int = 20):
            raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)

        def close(self) -> None:
            pass

    monkeypatch.setattr(ingestion_service, "BirdeyeClient", RejectedKeyClient)
    service = IngestionService()

    assert service._ingest_birdeye_solana(session) == 0

    health = session.scalars(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "source:birdeye")
        .order_by(models.SystemHealth.ts.desc())
    ).first()
    assert health is not None
    assert health.state == "red"
    assert "misconfigured" in (health.message or "")
    assert "BIRDEYE_API_KEY" in (health.message or "")
    assert "401" in (health.message or "")
    assert health.error_count == 0


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
    monkeypatch.setattr(service, "_ingest_solana_safety_snapshots", lambda session: 0)
    monkeypatch.setattr(service, "_ingest_evm_holder_snapshots", lambda session: 0)
    monkeypatch.setattr(service, "_run_data_quality_check", lambda session, decision_ts: {})
    monkeypatch.setattr(service, "_run_contract_analysis", lambda session, decision_ts: {})
    monkeypatch.setattr(ingestion_service, "run_mempool", lambda session, decision_ts=None: {})
    monkeypatch.setattr(ingestion_service, "run_narrative", lambda session, decision_ts=None: {})
    monkeypatch.setattr(ingestion_service, "extract_catalysts", lambda session, decision_ts=None: 0)
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
    scan = session.scalar(select(models.ScanResult).order_by(models.ScanResult.ts.desc()).limit(1))
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
