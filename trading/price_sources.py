"""Price sources backed by persisted evidence (no invented prices)."""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from storage import models
from trading.executors import PriceQuote

_CHAIN_SLUGS = {"solana": "sol", "base": "base", "ethereum": "eth"}


def token_url(chain: str, address: str) -> str:
    """Canonical GMGN token page for a chain (exact mapping from the spec)."""
    slug = _CHAIN_SLUGS.get(chain)
    if slug is None:
        raise ValueError(f"unknown chain: {chain}")
    return f"https://gmgn.ai/{slug}/token/{address}"


class MarketSnapshotPriceSource:
    """Latest persisted pool price for the asset (evidence, not a guess)."""

    source = "market_snapshot"

    def __init__(self, session: Session) -> None:
        self.session = session

    def price(self, mint: str, chain: str) -> PriceQuote | None:
        row = self.session.execute(
            select(models.MarketSnapshot.price_usd, models.MarketSnapshot.ts)
            .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
            .join(models.Asset, models.Asset.id == models.Pair.base_asset_id)
            .join(models.Chain, models.Chain.id == models.Asset.chain_id)
            .where(
                models.Asset.address == mint,
                models.Chain.slug == chain,
                models.MarketSnapshot.price_usd.is_not(None),
                models.MarketSnapshot.price_usd > 0,
            )
            .order_by(models.MarketSnapshot.ts.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        price, ts = row
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return PriceQuote(price=Decimal(str(price)), ts=ts, source=self.source)
