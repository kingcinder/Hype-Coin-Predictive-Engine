"""DexScreener refresh stage: re-fetch known live pairs once a day.

Discovery-only ingestion leaves every known pair to go stale, so only ~1.5%
of feature points ever get a price and the forecast trainer pool is frozen.
These tests cover the refresh stage's selection rules:

- only the current UTC hour's bucket is refreshed (hour-bucket rotation),
- only assets whose latest DexScreener snapshot is inside the alive window
  are candidates (the graveyard is skipped),
- assets snapshotted within the last hour are skipped (hourly buckets),
- a dead token (token-pairs returns []) does not stall the stage,
- the stage is a no-op when the per-scan cap is 0.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

import ingestion.service as ingestion_service
from common.config import get_settings
from ingestion.service import IngestionService
from storage import models
from storage.repository import (
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

QUOTE_ADDRESS = "USDC111111111111111111111111111111111111"


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


class FakeDexScreener:
    """Stand-in for DexScreenerClient: canned token-pairs per token address."""

    def __init__(self, payloads: dict[str, list[dict]]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, str]] = []

    def token_pairs(self, chain_slug: str, token_address: str) -> list[dict]:
        self.calls.append((chain_slug, token_address))
        return self.payloads.get(token_address, [])

    def close(self) -> None:  # noqa: D102 - test double
        return None


def _seed_asset(session, *, address: str, symbol: str) -> tuple[models.Asset, models.Pair, str]:
    """Asset with one pair and no snapshots yet; returns (asset, pair, pair_address)."""
    chain, _source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=address,
        symbol=symbol,
        name=f"{symbol} Fixture",
        first_seen_at=datetime(2026, 4, 30, tzinfo=UTC),
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address=QUOTE_ADDRESS,
        symbol="USDC",
        name="USD Coin",
        first_seen_at=datetime(2025, 5, 1, tzinfo=UTC),
    )
    pair_address = f"Pair{address[:20]}"
    _pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=pair_address,
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=datetime(2026, 4, 30, tzinfo=UTC),
    )
    session.commit()
    return asset, pair, pair_address


def _add_snapshot(session, *, pair: models.Pair, ts: datetime) -> None:
    _chain, source = seed_reference(session)
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=ts,
        observed_at=ts,
        price_usd=1.0,
        volume_usd=10_000,
        buys=20,
        sells=5,
        trades=25,
    )
    session.commit()


def _snapshot_count(session, pair_id: int) -> int:
    return session.scalar(
        select(func.count())
        .select_from(models.MarketSnapshot)
        .where(models.MarketSnapshot.pair_id == pair_id)
    )


def _run_refresh(
    session, monkeypatch, *, now: datetime, payloads: dict[str, list[dict]]
) -> tuple[int, FakeDexScreener]:
    monkeypatch.setattr(ingestion_service, "utc_now", lambda: now)
    fake = FakeDexScreener(payloads)
    monkeypatch.setattr(ingestion_service, "DexScreenerClient", lambda: fake)
    service = IngestionService()
    count = service._ingest_dexscreener_refresh(session)
    session.commit()
    return count, fake


def test_refresh_stores_new_snapshots_for_due_assets(session, monkeypatch) -> None:
    asset, pair, pair_address = _seed_asset(
        session, address="TokenAAA1111111111111111111111111111", symbol="AAA"
    )
    now = datetime(2026, 5, 2, asset.id % 24, 0, tzinfo=UTC)
    _add_snapshot(session, pair=pair, ts=now - timedelta(hours=5))
    payload = _pair_payload(pair_address=pair_address, token_address=asset.address, symbol="AAA")
    count, fake = _run_refresh(session, monkeypatch, now=now, payloads={asset.address: [payload]})
    assert fake.calls == [("solana", asset.address)]
    assert count == 1
    assert _snapshot_count(session, pair.id) == 2
    assert (
        session.scalar(
            select(models.SystemHealth).where(
                models.SystemHealth.component == "source:dexscreener_refresh"
            )
        ).state
        == "ok"
    )


def test_refresh_skips_graveyard_assets(session, monkeypatch) -> None:
    asset, pair, pair_address = _seed_asset(
        session, address="TokenBBB1111111111111111111111111111", symbol="BBB"
    )
    now = datetime(2026, 5, 2, asset.id % 24, 0, tzinfo=UTC)
    _add_snapshot(session, pair=pair, ts=now - timedelta(hours=100))
    payload = _pair_payload(pair_address=pair_address, token_address=asset.address, symbol="BBB")
    count, fake = _run_refresh(session, monkeypatch, now=now, payloads={asset.address: [payload]})
    assert fake.calls == []
    assert count == 0
    assert _snapshot_count(session, pair.id) == 1


def test_refresh_skips_fresh_assets(session, monkeypatch) -> None:
    asset, pair, pair_address = _seed_asset(
        session, address="TokenCCC1111111111111111111111111111", symbol="CCC"
    )
    now = datetime(2026, 5, 2, asset.id % 24, 0, tzinfo=UTC)
    _add_snapshot(session, pair=pair, ts=now - timedelta(minutes=30))
    payload = _pair_payload(pair_address=pair_address, token_address=asset.address, symbol="CCC")
    count, fake = _run_refresh(session, monkeypatch, now=now, payloads={asset.address: [payload]})
    assert fake.calls == []
    assert count == 0
    assert _snapshot_count(session, pair.id) == 1


def test_refresh_only_current_hour_bucket(session, monkeypatch) -> None:
    seeded = [
        _seed_asset(
            session, address=f"Token{i:02d}1111111111111111111111111111", symbol=f"T{i:02d}"
        )
        for i in range(30)
    ]
    bucket_hour = seeded[0][0].id % 24
    now = datetime(2026, 5, 2, bucket_hour, 0, tzinfo=UTC)
    for _asset, pair, _addr in seeded:
        _add_snapshot(session, pair=pair, ts=now - timedelta(hours=5))
    expected = {a.address for a, _p, _pa in seeded if a.id % 24 == bucket_hour}
    assert expected, "test setup: no asset fell in the chosen bucket"
    assert len(expected) < len(seeded), "test setup: every asset fell in one bucket"
    payloads = {
        a.address: [_pair_payload(pair_address=pa, token_address=a.address, symbol=a.symbol)]
        for a, _p, pa in seeded
    }
    count, fake = _run_refresh(session, monkeypatch, now=now, payloads=payloads)
    assert {token for _chain, token in fake.calls} == expected
    assert count == len(expected)


def test_refresh_continues_past_dead_token(session, monkeypatch) -> None:
    seeded = [
        _seed_asset(session, address=f"TokenD{i:02d}11111111111111111111111111", symbol=f"D{i:02d}")
        for i in range(25)
    ]
    # Two assets sharing one bucket (pigeonhole: 25 assets, 24 buckets).
    by_bucket: dict[int, list] = {}
    for item in seeded:
        by_bucket.setdefault(item[0].id % 24, []).append(item)
    shared = next(items for items in by_bucket.values() if len(items) >= 2)
    (dead, _dp, _da), (live, live_pair, live_pa) = shared[0], shared[1]
    now = datetime(2026, 5, 2, dead.id % 24, 0, tzinfo=UTC)
    for _asset, pair, _addr in seeded:
        _add_snapshot(session, pair=pair, ts=now - timedelta(hours=5))
    payload = _pair_payload(pair_address=live_pa, token_address=live.address, symbol=live.symbol)
    count, fake = _run_refresh(session, monkeypatch, now=now, payloads={live.address: [payload]})
    called = {token for _chain, token in fake.calls}
    assert dead.address in called, "dead token must still be attempted (it ages out on its own)"
    assert live.address in called, (
        "live token in the same bucket must not be starved by the dead one"
    )
    assert count == 1
    assert _snapshot_count(session, live_pair.id) == 2


def test_refresh_disabled_when_zero(session, monkeypatch) -> None:
    asset, pair, pair_address = _seed_asset(
        session, address="TokenFFF1111111111111111111111111111", symbol="FFF"
    )
    now = datetime(2026, 5, 2, asset.id % 24, 0, tzinfo=UTC)
    _add_snapshot(session, pair=pair, ts=now - timedelta(hours=5))
    monkeypatch.setattr(get_settings(), "dexscreener_refresh_assets_per_scan", 0)
    payload = _pair_payload(pair_address=pair_address, token_address=asset.address, symbol="FFF")
    count, fake = _run_refresh(session, monkeypatch, now=now, payloads={asset.address: [payload]})
    assert fake.calls == []
    assert count == 0
