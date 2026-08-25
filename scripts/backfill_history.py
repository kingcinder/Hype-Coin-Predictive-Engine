"""Backfill historical daily prices into MarketSnapshot for dense backtests.

The walk-forward backtest needs point-in-time price history to compute
forward returns, but a fresh deployment only accumulates snapshots from the
moment it starts scanning.  This script fetches historical daily closes from
CoinGecko (or DeFiLlama) for every tracked asset and inserts MarketSnapshot
rows with ``observed_at == ts`` — point-in-time correct, so backtests over
the window see the data as it would have been known.

Usage::

    python scripts/backfill_history.py --days 90 --provider coingecko
    python scripts/backfill_history.py --days 90 --provider defillama --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import select

from common.logging import get_logger
from storage import models
from storage.database import SessionLocal
from storage.repository import get_or_create_source, insert_market_snapshot_once

log = get_logger(__name__)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DEFILLAMA_COINS_BASE = "https://coins.llama.fi"

# Chain slug -> CoinGecko platform id / DeFiLlama chain name
PLATFORM_BY_CHAIN = {
    "solana": "solana",
    "base": "base",
    "ethereum": "ethereum",
}


def _assets_with_pairs(session) -> list[tuple[models.Asset, models.Pair, str]]:
    """(asset, pair, chain_slug) for every asset that has a tradable pair."""
    rows = session.execute(
        select(models.Asset, models.Pair, models.Chain.slug)
        .join(models.Pair, models.Pair.base_asset_id == models.Asset.id)
        .join(models.Chain, models.Chain.id == models.Asset.chain_id)
    ).all()
    return [(asset, pair, slug) for asset, pair, slug in rows]


def _coingecko_ids_from_evidence(session) -> dict[int, str]:
    """Map asset_id -> CoinGecko coin id from stored crawler evidence."""
    out: dict[int, str] = {}
    source = session.scalar(select(models.Source).where(models.Source.name == "coingecko"))
    if source is None:
        return out
    rows = session.scalars(
        select(models.RawEvidenceItem).where(models.RawEvidenceItem.source_id == source.id)
    ).all()
    for row in rows:
        payload = row.payload or {}
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            metrics = item.get("metrics") or {}
            coin_id = metrics.get("coingecko_id") or item.get("coingecko_id")
            symbol = (item.get("symbol") or "").lower()
            if coin_id and symbol:
                # Find the asset by symbol; store the first id we see.
                asset = session.scalar(
                    select(models.Asset).where(models.Asset.symbol.ilike(symbol)).limit(1)
                )
                if asset is not None:
                    out.setdefault(asset.id, str(coin_id))
    return out


def _resolve_coingecko_ids(
    session, assets: list[tuple[models.Asset, models.Pair, str]], client: httpx.Client
) -> dict[int, str]:
    """asset_id -> CoinGecko coin id, using evidence first then a symbol search."""
    ids = _coingecko_ids_from_evidence(session)
    for asset, _, _ in assets:
        if asset.id in ids:
            continue
        try:
            resp = client.get(
                f"{COINGECKO_BASE}/search",
                params={"query": asset.symbol},
                timeout=15.0,
            )
            resp.raise_for_status()
            coins = resp.json().get("coins") or []
            if coins:
                ids[asset.id] = str(coins[0]["id"])
            time.sleep(1.0)  # free tier rate limit
        except Exception as exc:  # noqa: BLE001 - one asset failing must not abort the run
            log.warning("coingecko_resolve_failed", symbol=asset.symbol, error=str(exc))
    return ids


def _coingecko_history(
    client: httpx.Client, coin_id: str, days: int
) -> list[tuple[datetime, float]]:
    """Daily closes from CoinGecko market_chart: [(ts, price), ...]."""
    resp = client.get(
        f"{COINGECKO_BASE}/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": days, "interval": "daily"},
        timeout=30.0,
    )
    resp.raise_for_status()
    out: list[tuple[datetime, float]] = []
    for ts_ms, price in resp.json().get("prices", []):
        if price is None:
            continue
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        out.append((ts, float(price)))
    return out


def backfill_coingecko(session, *, days: int = 90, dry_run: bool = False) -> dict[str, int | bool]:
    """Backfill daily closes from CoinGecko for all assets with pairs."""
    client = httpx.Client(
        headers={"User-Agent": "serpent-backfill/1.0"}, follow_redirects=True, timeout=30.0
    )
    source = get_or_create_source(
        session,
        name="coingecko",
        source_type="market_data",
        tier="public_metadata",
        base_url=COINGECKO_BASE,
    )
    assets = _assets_with_pairs(session)
    ids = _resolve_coingecko_ids(session, assets, client)
    inserted = 0
    errors = 0
    covered = 0
    for asset, pair, _ in assets:
        coin_id = ids.get(asset.id)
        if not coin_id:
            continue
        try:
            history = _coingecko_history(client, coin_id, days)
            time.sleep(1.0)  # free tier rate limit
        except Exception as exc:  # noqa: BLE001
            log.warning("coingecko_history_failed", symbol=asset.symbol, error=str(exc))
            errors += 1
            continue
        for ts, price in history:
            if not dry_run:
                insert_market_snapshot_once(
                    session,
                    pair_id=pair.id,
                    source_id=source.id,
                    ts=ts,
                    observed_at=ts,
                    price_usd=price,
                )
            inserted += 1
        if history:
            covered += 1
        if not dry_run:
            session.commit()
    client.close()
    return {
        "provider": "coingecko",
        "assets_with_pairs": len(assets),
        "assets_covered": covered,
        "snapshots_inserted": inserted,
        "resolve_errors": errors,
        "dry_run": dry_run,
    }


def _defillama_history(
    client: httpx.Client, coin_refs: list[str], days: int
) -> dict[str, list[tuple[datetime, float]]]:
    """Per-day batched closes from DeFiLlama coins API.

    One request per day across ALL coins (comma-joined), which is far fewer
    requests than per-asset polling.
    """
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    per_coin: dict[str, list[tuple[datetime, float]]] = {ref: [] for ref in coin_refs}
    for offset in range(days, 0, -1):
        day = today - timedelta(days=offset)
        ts = int(day.timestamp())
        url = f"{DEFILLAMA_COINS_BASE}/prices/historical/{ts}/{','.join(coin_refs)}"
        try:
            resp = client.get(url, timeout=30.0)
            resp.raise_for_status()
            data = resp.json().get("coins", {})
        except Exception as exc:  # noqa: BLE001
            log.warning("defillama_day_failed", day=day.date().isoformat(), error=str(exc))
            continue
        for ref in coin_refs:
            coin = data.get(ref)
            price = (coin or {}).get("price")
            if price is not None:
                per_coin[ref].append((day, float(price)))
        time.sleep(0.5)
    return per_coin


def backfill_defillama(session, *, days: int = 90, dry_run: bool = False) -> dict[str, int | bool]:
    """Backfill daily closes from DeFiLlama (chain:address refs) — good for
    EVM/Solana addresses CoinGecko may not index."""
    client = httpx.Client(
        headers={"User-Agent": "serpent-backfill/1.0"}, follow_redirects=True, timeout=30.0
    )
    source = get_or_create_source(
        session,
        name="defillama",
        source_type="market_data",
        tier="public_metadata",
        base_url=DEFILLAMA_COINS_BASE,
    )
    assets = _assets_with_pairs(session)
    ref_to_asset: dict[str, tuple[models.Asset, models.Pair]] = {}
    for asset, pair, slug in assets:
        platform = PLATFORM_BY_CHAIN.get(slug)
        if platform and asset.address:
            ref_to_asset[f"{platform}:{asset.address.lower()}"] = (asset, pair)
    history = _defillama_history(client, list(ref_to_asset.keys()), days)
    inserted = 0
    covered = 0
    for ref, rows in history.items():
        asset, pair = ref_to_asset[ref]
        if rows:
            covered += 1
        for ts, price in rows:
            if not dry_run:
                insert_market_snapshot_once(
                    session,
                    pair_id=pair.id,
                    source_id=source.id,
                    ts=ts,
                    observed_at=ts,
                    price_usd=price,
                )
            inserted += 1
        if not dry_run:
            session.commit()
    client.close()
    return {
        "provider": "defillama",
        "assets_with_pairs": len(assets),
        "assets_covered": covered,
        "snapshots_inserted": inserted,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill historical daily prices into MarketSnapshot for backtests."
    )
    parser.add_argument("--days", type=int, default=90, help="days of history to fetch")
    parser.add_argument(
        "--provider",
        choices=["coingecko", "defillama"],
        default="coingecko",
        help="price history provider",
    )
    parser.add_argument("--dry-run", action="store_true", help="count inserts without writing")
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        if args.provider == "coingecko":
            result: dict[str, Any] = backfill_coingecko(
                session, days=args.days, dry_run=args.dry_run
            )
        else:
            result = backfill_defillama(session, days=args.days, dry_run=args.dry_run)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
