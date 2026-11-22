"""EVM first-buy RPC fallback wiring + catch-up pass.

Covers the live path (GeckoTerminal fails for a Base pool -> the EVM RPC
fallback is used; a backed-off GeckoTerminal still lets EVM pools use their
RPC) and the catch-up pass (a Base pair whose window closed unobserved gets
its first buys rebuilt from Swap logs).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

import ingestion.service as ingestion_service
from ingestion.normalizers import NormalizedPair
from ingestion.service import IngestionService
from storage import models
from storage.repository import upsert_asset, upsert_pool_and_pair
from tests.conftest import seed_reference

BASE_POOL = "0x" + "33" * 20
BASE_TOKEN = "0x" + "00" * 19 + "01"
WETH_BASE = "0x4200000000000000000000000000000000000006"
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _pair(address: str, chain: str, now: datetime) -> NormalizedPair:
    return NormalizedPair(
        chain_slug=chain,
        dex_id="raydium" if chain == "solana" else "uniswap-v3",
        pair_address=address,
        base_address=("MintBudget" if chain == "solana" else BASE_TOKEN),
        base_symbol="BUDGET",
        base_name="Budget Token",
        quote_address=WETH_BASE,
        quote_symbol="WETH",
        quote_name="Wrapped Ether",
        pair_created_at=now,
        price_usd=0.001,
        volume_usd=0.0,
        liquidity_usd=80000.0,
        reserve_base=None,
        reserve_quote=None,
        buys=0,
        sells=0,
        trades=0,
    )


def _rate_limit_429(pool: str) -> None:
    request = httpx.Request("GET", f"https://api.geckoterminal.com/{pool}")
    response = httpx.Response(429, request=request)
    raise httpx.HTTPStatusError("429", request=request, response=response)


def test_evm_rpc_fallback_on_gecko_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 for a Base pool rebuilds its first buys from the EVM RPC."""
    service = IngestionService()
    monkeypatch.setattr(
        ingestion_service.GeckoTerminalClient,
        "pool_trades",
        lambda self, chain, pool: _rate_limit_429(pool),
    )
    swap = {"wallet": "0xbuyer", "side": "buy", "size_usd": 0.5, "ts": NOW + timedelta(seconds=60)}
    calls: list[dict] = []

    def fake_fetch(**kwargs: object) -> list[dict]:
        calls.append(kwargs)
        return [swap]

    monkeypatch.setattr(ingestion_service, "fetch_evm_pool_swaps", fake_fetch)
    swaps, known = service._first_buy_swaps(_pair(BASE_POOL, "base", NOW), NOW, set())
    assert swaps == [swap]
    assert known is True
    assert calls and calls[0]["chain_slug"] == "base"
    assert calls[0]["pool_address"] == BASE_POOL
    assert service._first_buy_stats["evm_rpc_fallback_hits"] == 1
    assert service._first_buy_stats["evm_rpc_fallback_swaps"] == 1


def test_no_evm_rpc_fallback_when_gecko_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful (even empty) GeckoTerminal feed never touches the RPC."""
    service = IngestionService()
    monkeypatch.setattr(
        ingestion_service.GeckoTerminalClient, "pool_trades", lambda self, chain, pool: []
    )
    called: list[int] = []
    monkeypatch.setattr(
        ingestion_service, "fetch_evm_pool_swaps", lambda **kwargs: called.append(1) or []
    )
    swaps, known = service._first_buy_swaps(_pair(BASE_POOL, "base", NOW), NOW, set())
    assert (swaps, known) == ([], True)
    assert called == []
    assert service._first_buy_stats["evm_rpc_fallback_hits"] == 0


def test_evm_pool_gets_rpc_fallback_while_gecko_backed_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backed-off GeckoTerminal still lets an EVM pool use its own RPC."""
    service = IngestionService()
    service._first_buy_provider_backoff = True
    gecko_calls: list[str] = []
    monkeypatch.setattr(
        ingestion_service.GeckoTerminalClient,
        "pool_trades",
        lambda self, chain, pool: gecko_calls.append(pool) or [],
    )
    swap = {"wallet": "0xbuyer", "side": "buy", "size_usd": 0.5, "ts": NOW + timedelta(seconds=60)}
    monkeypatch.setattr(ingestion_service, "fetch_evm_pool_swaps", lambda **kwargs: [swap])
    swaps, known = service._first_buy_swaps(_pair(BASE_POOL, "base", NOW), NOW, set())
    assert swaps == [swap]
    assert known is True
    assert gecko_calls == []  # GeckoTerminal was never attempted.
    assert service._first_buy_stats["evm_rpc_fallback_hits"] == 1
    assert service._first_buy_stats["backoff_skipped"] == 0


def test_solana_pool_skipped_while_gecko_backed_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff still skips Solana pools (existing behaviour, unchanged)."""
    service = IngestionService()
    service._first_buy_provider_backoff = True
    swaps, known = service._first_buy_swaps(_pair("PoolSol1", "solana", NOW), NOW, set())
    assert (swaps, known) == ([], False)
    assert service._first_buy_stats["backoff_skipped"] == 1


def _seed_base_pair(
    session, *, created: datetime, pool_address: str, base_address: str
) -> models.Asset:
    """A Base pair (assets + pool + pair) with the given creation time."""
    chain = session.scalar(select(models.Chain).where(models.Chain.slug == "base"))
    assert chain is not None
    base_asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=base_address,
        symbol="TOK",
        name="Tok",
        first_seen_at=created,
    )
    quote_asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=WETH_BASE,
        symbol="WETH",
        name="Wrapped Ether",
        first_seen_at=created - timedelta(days=365),
    )
    upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="uniswap-v3",
        pair_address=pool_address,
        base_asset_id=base_asset.id,
        quote_asset_id=quote_asset.id,
        created_at_source=created,
        first_available_at=created,
    )
    session.commit()
    return base_asset


def test_catch_up_evm_first_buys_recovers_closed_window(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Base pair whose window closed unobserved gets its buys rebuilt."""
    seed_reference(session)
    service = IngestionService()
    service.ensure_reference_data(session)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: NOW)
    monkeypatch.setattr("trading.wallets.load_wallets", lambda: [])
    created = NOW - timedelta(hours=1)  # Window closed 55 minutes ago.
    base_asset = _seed_base_pair(
        session, created=created, pool_address=BASE_POOL, base_address=BASE_TOKEN
    )
    # Never observed: stats row exists with buys NULL, last attempt 7h ago
    # (past the 6h retry guard).
    session.add(
        models.PairFirstWindowStats(
            asset_id=base_asset.id,
            window_minutes=5,
            buys=None,
            sells=None,
            buy_sell_ratio=None,
            computed_at=NOW - timedelta(hours=7),
        )
    )
    session.commit()
    swap = {
        "wallet": "0xbuyer",
        "side": "buy",
        "size_usd": 0.5,
        "ts": created + timedelta(seconds=60),
    }
    monkeypatch.setattr(ingestion_service, "fetch_evm_pool_swaps", lambda **kwargs: [swap])
    assert service._catch_up_evm_first_buys(session) == 1
    session.commit()
    stats = session.scalar(
        select(models.PairFirstWindowStats).where(
            models.PairFirstWindowStats.asset_id == base_asset.id
        )
    )
    assert stats is not None and stats.buys == 1 and stats.sells == 0
    assert service._first_buy_stats["catchup_observed"] == 1
    assert service._first_buy_stats["catchup_swaps"] == 1
    first_buy = session.scalar(
        select(models.FirstBuy).where(models.FirstBuy.asset_id == base_asset.id)
    )
    assert first_buy is not None and first_buy.wallet == "0xbuyer"


def test_catch_up_skips_already_observed_pair(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pair whose window was already observed is never re-fetched."""
    seed_reference(session)
    service = IngestionService()
    service.ensure_reference_data(session)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: NOW)
    monkeypatch.setattr("trading.wallets.load_wallets", lambda: [])
    created = NOW - timedelta(hours=1)
    base_asset = _seed_base_pair(
        session, created=created, pool_address=BASE_POOL, base_address=BASE_TOKEN
    )
    session.add(
        models.PairFirstWindowStats(
            asset_id=base_asset.id,
            window_minutes=5,
            buys=0,
            sells=0,
            buy_sell_ratio=None,
            computed_at=NOW - timedelta(hours=7),
        )
    )
    session.commit()
    called: list[int] = []
    monkeypatch.setattr(
        ingestion_service, "fetch_evm_pool_swaps", lambda **kwargs: called.append(1) or []
    )
    assert service._catch_up_evm_first_buys(session) == 0
    assert called == []


def test_catch_up_skips_open_window_pair(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pair whose window is still open is left to the normal path."""
    seed_reference(session)
    service = IngestionService()
    service.ensure_reference_data(session)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: NOW)
    monkeypatch.setattr("trading.wallets.load_wallets", lambda: [])
    created = NOW - timedelta(minutes=2)  # Window closes in 3 minutes.
    base_asset = _seed_base_pair(
        session, created=created, pool_address=BASE_POOL, base_address=BASE_TOKEN
    )
    session.add(
        models.PairFirstWindowStats(
            asset_id=base_asset.id,
            window_minutes=5,
            buys=None,
            sells=None,
            buy_sell_ratio=None,
            computed_at=NOW - timedelta(hours=7),
        )
    )
    session.commit()
    called: list[int] = []
    monkeypatch.setattr(
        ingestion_service, "fetch_evm_pool_swaps", lambda **kwargs: called.append(1) or []
    )
    assert service._catch_up_evm_first_buys(session) == 0
    assert called == []
