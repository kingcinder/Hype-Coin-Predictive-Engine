"""EVM first-buy fairness: Base/Ethereum pools must get trades fetches.

Regression coverage for the EVM first-buy blind spot (0 EVM first-buys ever,
26,899 all Solana). Root cause: the first-buy path consumed the shared
GeckoTerminal rate limit (~5 trades fetches per scan) in strict chain order
(Solana first), so Base/Ethereum pools were never reached, and a rate limit
opened during Solana storage aborted new-pool discovery for the remaining
chains, so EVM pairs arrived after the 5-minute first-buy window had closed.

The fix: per-chain first-buy budgets, discovery for all chains before any
storage, and round-robin interleaved storage so the first fetches are spread
fairly across chains.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

import ingestion.service as ingestion_service
from common.config import get_settings
from ingestion.normalizers import NormalizedPair
from ingestion.provider_gate import CircuitOpen
from ingestion.service import IngestionService, _interleave_by_chain
from ingestion.source_clients import GeckoTerminalClient, reset_geckoterminal_gate
from storage import models
from tests.conftest import seed_reference


@pytest.fixture(autouse=True)
def _reset_gecko_gate():
    reset_geckoterminal_gate()
    yield
    reset_geckoterminal_gate()


def _pair(address: str, chain: str, now: datetime) -> NormalizedPair:
    return NormalizedPair(
        chain_slug=chain,
        dex_id="raydium" if chain == "solana" else "uniswap-v3",
        pair_address=address,
        base_address=(
            "MintBudget" if chain == "solana" else "0xBaseToken000000000000000000000000000001"
        ),
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


def test_first_buy_trade_budget_is_per_chain(monkeypatch) -> None:
    """A busy Solana must not starve Base of first-buy fetch slots."""
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(get_settings(), "first_buy_trade_max_pools_per_scan", 2)
    calls: list[str] = []
    monkeypatch.setattr(
        ingestion_service.GeckoTerminalClient,
        "pool_trades",
        lambda self, chain, pool: calls.append(pool) or [],
    )

    service = IngestionService()
    # Solana fills its own cap first (old behaviour would deny Base next).
    assert service._first_buy_swaps(_pair("PoolSol1", "solana", now), now, set()) == ([], True)
    assert service._first_buy_swaps(_pair("PoolSol2", "solana", now), now, set()) == ([], True)
    assert service._first_buy_swaps(_pair("PoolSol3", "solana", now), now, set()) == ([], False)
    # Base has its own budget: still fetched despite Solana hitting the cap.
    assert service._first_buy_swaps(_pair("PoolBase1", "base", now), now, set()) == ([], True)

    assert calls == ["PoolSol1", "PoolSol2", "PoolBase1"]
    assert service._first_buy_stats["pools"] == 3
    assert service._first_buy_stats["pools_by_chain"] == {"solana": 2, "base": 1}
    assert service._first_buy_stats["budget_skipped"] == 1


def test_interleave_by_chain_round_robins_uneven_lists() -> None:
    items = [
        ("solana", {"id": "s1"}),
        ("solana", {"id": "s2"}),
        ("solana", {"id": "s3"}),
        ("base", {"id": "b1"}),
        ("ethereum", {"id": "e1"}),
        ("ethereum", {"id": "e2"}),
    ]
    assert [payload["id"] for _, payload in _interleave_by_chain(items)] == [
        "s1",
        "b1",
        "e1",
        "s2",
        "e2",
        "s3",
    ]
    assert _interleave_by_chain([]) == []


def _gecko_pool(chain: str, pool_address: str, base_address: str, now: datetime) -> dict:
    quote = (
        "0x4200000000000000000000000000000000000006"
        if chain == "base"
        else "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    )
    quote_symbol = "WETH"
    return {
        "id": f"{chain}_{pool_address}",
        "type": "pool",
        "attributes": {
            "address": pool_address,
            "name": f"TOK / {quote_symbol}",
            "pool_created_at": now.isoformat(),
            "base_token_price_usd": "0.001",
            "reserve_in_usd": "12000",
            "transactions": {"h1": {"buys": 5, "sells": 0}},
            "volume_usd": {"h1": "130"},
        },
        "relationships": {
            "base_token": {"data": {"id": f"{chain}_{base_address}", "type": "token"}},
            "quote_token": {"data": {"id": f"{chain}_{quote}", "type": "token"}},
            "dex": {"data": {"id": "uniswap-v3", "type": "dex"}},
        },
    }


def test_evm_pool_gets_first_buy_fetch_before_rate_limit(session, monkeypatch) -> None:
    """Interleaved storage: a Base pool is fetched before the limit opens.

    Three Solana pools and two Base pools are all in-window. The provider
    rate-limits on the 4th trades call, so without interleaving the Base
    pools would never be reached. With it, the first Base pool is fetched
    second and its first-window stats are observed (buys == 0, not unknown).
    """
    seed_reference(session)
    service = IngestionService()
    service.ensure_reference_data(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    monkeypatch.setattr(get_settings(), "target_chains_csv", "solana,base,ethereum")
    monkeypatch.setattr("trading.wallets.load_wallets", lambda: [])

    pools = {
        "solana": [
            _gecko_pool("solana", "PoolSol1", "MintSol1", now),
            _gecko_pool("solana", "PoolSol2", "MintSol2", now),
            _gecko_pool("solana", "PoolSol3", "MintSol3", now),
        ],
        "base": [
            _gecko_pool(
                "base",
                "0xBasePool10000000000000000000000000000001",
                "0xBaseToken00000000000000000000000000000001",
                now,
            ),
            _gecko_pool(
                "base",
                "0xBasePool20000000000000000000000000000002",
                "0xBaseToken00000000000000000000000000000002",
                now,
            ),
        ],
        "ethereum": [],
    }

    def new_pools(self, chain_slug: str):
        return pools[chain_slug]

    monkeypatch.setattr(GeckoTerminalClient, "new_pools", new_pools)
    monkeypatch.setattr(GeckoTerminalClient, "close", lambda self: None)

    calls: list[tuple[str, str]] = []

    def pool_trades(self, chain: str, pool: str):
        calls.append((chain, pool))
        if len(calls) >= 4:
            request = httpx.Request("GET", f"https://api.geckoterminal.com/{pool}")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("429", request=request, response=response)
        return []

    monkeypatch.setattr(GeckoTerminalClient, "pool_trades", pool_trades)

    assert service._ingest_geckoterminal_new_pools(session) == 5
    session.commit()

    # Base was fetched second, before the rate limit opened on call 4.
    assert calls == [
        ("solana", "PoolSol1"),
        ("base", "0xBasePool10000000000000000000000000000001"),
        ("solana", "PoolSol2"),
        ("base", "0xBasePool20000000000000000000000000000002"),
    ]
    assert service._first_buy_stats["pools_by_chain"]["base"] == 2
    assert service._first_buy_stats["rate_limit_failures"] == 1
    assert service._first_buy_stats["backoff_skipped"] == 1  # PoolSol3

    base_asset = session.scalar(
        select(models.Asset).where(
            models.Asset.address == "0xBaseToken00000000000000000000000000000001"
        )
    )
    assert base_asset is not None
    stats = session.scalar(
        select(models.PairFirstWindowStats).where(
            models.PairFirstWindowStats.asset_id == base_asset.id
        )
    )
    # Observed (empty feed), not unknown: the EVM first-buy window was seen.
    assert stats is not None and stats.buys == 0 and stats.sells == 0


def test_evm_discovery_not_deferred_by_solana_cooldown(session, monkeypatch) -> None:
    """A Solana cooldown must not defer Base discovery to a later scan."""
    seed_reference(session)
    service = IngestionService()
    service.ensure_reference_data(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    monkeypatch.setattr(get_settings(), "target_chains_csv", "solana,base,ethereum")
    monkeypatch.setattr("trading.wallets.load_wallets", lambda: [])

    base_pool = _gecko_pool(
        "base",
        "0xBasePool30000000000000000000000000000003",
        "0xBaseToken00000000000000000000000000000003",
        now,
    )

    def new_pools(self, chain_slug: str):
        if chain_slug == "solana":
            raise CircuitOpen("provider cooling down for 53.4s")
        return [base_pool] if chain_slug == "base" else []

    monkeypatch.setattr(GeckoTerminalClient, "new_pools", new_pools)
    monkeypatch.setattr(GeckoTerminalClient, "close", lambda self: None)
    monkeypatch.setattr(GeckoTerminalClient, "pool_trades", lambda self, chain, pool: [])

    assert service._ingest_geckoterminal_new_pools(session) == 1
    session.commit()

    pair = session.scalars(
        select(models.Pair)
        .join(models.Chain, models.Pair.chain_id == models.Chain.id)
        .where(models.Chain.slug == "base")
    ).one()
    assert pair is not None
    health = session.scalars(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "source:geckoterminal_new_pools")
        .order_by(models.SystemHealth.ts.desc())
    ).first()
    assert health is not None
    assert health.state == "yellow"
    assert "deferred" in (health.message or "")
    assert "solana" in (health.message or "")
