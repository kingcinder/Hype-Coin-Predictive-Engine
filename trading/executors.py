from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class PriceQuote:
    price: Decimal
    ts: datetime  # UTC
    source: str
    # Per-token price in the chain's native currency (SOL on Solana), when
    # the source reports it. ``price`` stays USD/token everywhere (stops,
    # targets, evidence, divergence checks all compare USD prices); the
    # native price exists only so trade sizing can buy a real token count:
    # qty = size_sol / price_native. Without it, qty = size_sol / price_usd
    # is SOL/USD too small (BANDANA CAT: 286,576,168 recorded vs ~19.67e9
    # real tokens at SOL $68.64). None when the source has no native price
    # (persisted snapshots) -- callers then fall back to the USD-ratio qty.
    price_native: Decimal | None = None


class PriceSource(Protocol):
    def price(self, mint: str, chain: str) -> PriceQuote | None: ...


class PaperExecutor:
    name = "paper"
