from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from storage import models
from storage.database import Base
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    with SessionLocal() as db:
        yield db


def seed_reference(session: Session):
    solana = get_or_create_chain(
        session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
    )
    source = get_or_create_source(
        session,
        name="dexscreener",
        source_type="market_data",
        tier="venue",
        base_url="https://api.dexscreener.com",
    )
    return solana, source


def seed_market_asset(
    session: Session,
    *,
    low_liquidity: bool = False,
    address: str = "Token111111111111111111111111111111111111",
    symbol: str = "HYPE",
    pair_address: str = "Pair111111111111111111111111111111111111",
) -> models.Asset:
    chain, source = seed_reference(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=address,
        symbol=symbol,
        name=f"{symbol} Fixture",
        first_seen_at=now - timedelta(hours=2),
        website_url="https://example.org",
        github_url="https://github.com/example/hype",
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="USDC111111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=now - timedelta(days=365),
    )
    pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=pair_address,
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=now - timedelta(hours=2),
    )
    prices = [1.0, 1.05, 1.10]
    for idx, price in enumerate(prices):
        ts = now - timedelta(hours=2 - idx)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=10_000 * (idx + 1),
            buys=20 + idx * 5,
            sells=5,
            trades=25 + idx * 5,
        )
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            reserve_usd=5_000 if low_liquidity else 100_000 + idx * 10_000,
        )
    session.commit()
    return asset
