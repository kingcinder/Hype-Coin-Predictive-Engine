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


class PriceSource(Protocol):
    def price(self, mint: str, chain: str) -> PriceQuote | None: ...


class PaperExecutor:
    name = "paper"
