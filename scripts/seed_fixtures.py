from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session

from scoring.engine import score_current_assets
from storage import models
from storage.database import Base, session_scope
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    record_health,
    upsert_asset,
    upsert_contract,
    upsert_pool_and_pair,
)


def seed_fixture_data(*, session: Session | None = None) -> None:
    """Seed the 2-token fixture fleet (assets, snapshots, holders, scores).

    ``session`` is injectable for tests: when a caller passes one, schema
    creation and all writes go through it and its lifecycle stays with the
    caller. When None — the CLI path — this call opens and owns its own
    ``session_scope()`` cycle, mirroring ``scripts/rescore.py``'s seam (the
    seam can never be reached from ``__main__``, which passes no session).
    """
    if session is not None:
        _seed_in_session(session)
        return
    with session_scope() as active:
        _seed_in_session(active)


def _seed_in_session(session: Session) -> None:
    """The fixture-seeding body against an active session (commits at the end)."""
    Base.metadata.create_all(bind=session.get_bind())
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    chain = get_or_create_chain(
        session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
    )
    source = get_or_create_source(
        session,
        name="dexscreener",
        source_type="market_data",
        tier="venue",
        base_url="https://api.dexscreener.com",
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="USDC111111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=now - timedelta(days=365),
    )
    candidates = [
        ("HYPE", "TokenHype111111111111111111111111111111", 120_000, [1.0, 1.04, 1.20], 0.15),
        ("DANGER", "TokenDanger1111111111111111111111111111", 4_000, [1.0, 1.8, 2.4], 0.92),
    ]
    asset_ids: list[int] = []
    for symbol, address, liquidity, prices, concentration in candidates:
        asset = upsert_asset(
            session,
            chain_id=chain.id,
            address=address,
            symbol=symbol,
            name=f"{symbol} fixture token",
            first_seen_at=now - timedelta(hours=3),
            website_url="https://example.org" if symbol == "HYPE" else None,
            github_url="https://github.com/example/hype" if symbol == "HYPE" else None,
        )
        contract = upsert_contract(
            session,
            chain_id=chain.id,
            asset_id=asset.id,
            address=address,
            observed_at=now - timedelta(hours=3),
            deployer_wallet=f"deployer-{symbol.lower()}",
        )
        pool, pair = upsert_pool_and_pair(
            session,
            chain_id=chain.id,
            dex_id="raydium",
            pair_address=f"Pair{symbol}111111111111111111111111111111",
            base_asset_id=asset.id,
            quote_asset_id=quote.id,
            created_at_source=now - timedelta(hours=3),
        )
        for idx, price in enumerate(prices):
            ts = now - timedelta(hours=2 - idx)
            insert_market_snapshot_once(
                session,
                pair_id=pair.id,
                source_id=source.id,
                ts=ts,
                observed_at=ts,
                price_usd=price,
                volume_usd=5_000 * (idx + 1) * (4 if symbol == "DANGER" else 1),
                buys=20 + idx * 15,
                sells=5 + idx,
                trades=25 + idx * 16,
            )
            insert_liquidity_snapshot_once(
                session,
                pool_id=pool.id,
                source_id=source.id,
                ts=ts,
                observed_at=ts,
                reserve_usd=liquidity,
            )
        if symbol == "DANGER":
            session.add(
                models.ContractFlag(
                    contract_id=contract.id,
                    source_id=source.id,
                    ts=now,
                    observed_at=now,
                    flag_type="mint_or_freeze_danger",
                    severity="critical",
                    details={"fixture": True},
                )
            )
        session.add(
            models.Holder(
                asset_id=asset.id,
                wallet_address=f"wallet-{symbol}-top",
                source_id=source.id,
                ts=now,
                observed_at=now,
                balance=1_000_000,
                pct_supply=concentration,
            )
        )
        asset_ids.append(asset.id)
    score_current_assets(session, decision_ts=now, asset_ids=asset_ids)
    record_health(
        session, component="fixture_seed", state="ok", message="fixture scores created", ts=now
    )
    session.commit()


if __name__ == "__main__":
    seed_fixture_data()
    print("Seeded fixture assets, snapshots, features, scores, alerts, and health.")
