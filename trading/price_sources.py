"""Price sources: live quotes first, persisted evidence as fallback.

A trade needs a quote younger than ``STALE_PRICE_SECONDS`` (60 s). Persisted
market snapshots are hourly-bucketed, so a snapshot-only source made every
paper trade refuse with ``stale_price`` -- the freshest snapshot is already
minutes old the instant it is written. The live DexScreener source (keyless
public API, already used by the engine's DexScreener crawler) gives a
genuinely fresh quote at trade time; the snapshot source stays as the
fallback when the live fetch fails. No price is ever invented: every quote
carries the time it was observed and the source that served it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.http import HttpClient
from common.logging import get_logger
from storage import models
from trading.executors import PriceQuote

log = get_logger(__name__)

_CHAIN_SLUGS = {"solana": "sol", "base": "base", "ethereum": "eth"}

# DexScreener chainId values match the engine's chain slugs.
_DEXSCREENER_CHAIN_IDS = {"solana": "solana", "base": "base", "ethereum": "ethereum"}

_DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
# A pair is a dust remnant (not a market) when BOTH its USD liquidity and
# its market value are below these floors. Both signals are required: a
# small pool can be legitimately thin while the token is live, and a pair
# can report no marketCap at all.
_DUST_LIQUIDITY_USD = Decimal("10")
_DUST_MCAP_USD = Decimal("1000")
_PRICE_CACHE_TTL_SECONDS = 10.0

_price_cache: dict[tuple[str, str], tuple[float, PriceQuote]] = {}


def _cache_get(cache: dict, key: tuple[str, str]) -> PriceQuote | None:
    entry = cache.get(key)
    if entry is None:
        return None
    stored_at, quote = entry
    if time.monotonic() - stored_at > _PRICE_CACHE_TTL_SECONDS:
        del cache[key]
        return None
    return quote


def _cache_set(cache: dict, key: tuple[str, str], quote: PriceQuote) -> None:
    cache[key] = (time.monotonic(), quote)


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


class DexScreenerLivePriceSource:
    """Live price from the keyless DexScreener public API.

    ``GET /latest/dex/tokens/{mint}`` returns every pair for the token. The
    pair for the requested chain with the best liveness score wins (see
    ``_pick_quote``): real USD liquidity first, marketCap for Meteora DBC
    pairs that report liquidity null, 24h volume last. Dust remnant pairs
    (tiny liquidity AND tiny market value) are skipped outright. The quote
    carries both priceUsd and priceNative and is stamped with the fetch
    time, so the 60 s staleness gate is meaningful. Any failure (network,
    no pair for the chain, unparseable price) returns ``None`` so callers
    fall back to persisted snapshots.
    """

    source = "dexscreener_live"

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(base_url=_DEXSCREENER_BASE_URL)

    def price(self, mint: str, chain: str) -> PriceQuote | None:
        chain_id = _DEXSCREENER_CHAIN_IDS.get(chain)
        if chain_id is None:
            return None
        cache_key = (chain, mint)
        cached = _cache_get(_price_cache, cache_key)
        if cached is not None:
            return cached
        try:
            result = self.http.get_json(f"/latest/dex/tokens/{mint}")
        except Exception:
            log.error("dexscreener_live_error", exc_info=True)
            return None
        quote = self._pick_quote(result, chain_id)
        if quote is not None:
            _cache_set(_price_cache, cache_key, quote)
        return quote

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        try:
            out = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return out if out.is_finite() else None

    @staticmethod
    def _pick_quote(result: object, chain_id: str) -> PriceQuote | None:
        """Pick the live pair for a chain.

        BANDANA CAT proved the old rule (highest ``liquidity.usd``) picks a
        dead pair: the engine quoted a $2.22-liquidity / $1-mcap Meteora
        remnant at 1.326e-09 while the live Meteora DBC pair (liquidity.usd
        null, mcap $191,662) sat at 0.0001916 -- the +300% target was smashed
        in reality and the engine could not see it. Three changes:

        * A null liquidity is never treated as 0. A DBC pair with no
          liquidity is ranked on its marketCap instead.
        * Dust remnants are skipped outright: a pair is dust only when
          BOTH its USD liquidity and its market value are tiny, so a
          legitimately small pool (large mcap) or a pair that simply
          reports no marketCap is never rejected on one signal alone.
        * 24h volume is the last-resort ranking signal, never the first:
          a remnant pool can out-volume the live pair on wash trades
          (BANDANA CAT: dust $43,833 vs live $3,882). MarketCap
          (price x supply) is the better liveness proxy for one token:
          the pair whose price implies the larger market value is live.
        """
        if not isinstance(result, dict):
            return None
        pairs = result.get("pairs")
        if not isinstance(pairs, list):
            return None
        best: tuple[Decimal, Decimal, Decimal | None] | None = None
        for pair in pairs:
            if not isinstance(pair, dict) or pair.get("chainId") != chain_id:
                continue
            price = DexScreenerLivePriceSource._decimal(pair.get("priceUsd"))
            if price is None or price <= 0:
                continue
            liquidity = pair.get("liquidity") or {}
            liq_usd = (
                DexScreenerLivePriceSource._decimal(liquidity.get("usd"))
                if isinstance(liquidity, dict)
                else None
            )
            mcap = DexScreenerLivePriceSource._decimal(pair.get("marketCap"))
            if mcap is None:
                mcap = DexScreenerLivePriceSource._decimal(pair.get("fdv"))
            volume = pair.get("volume") or {}
            vol24 = (
                DexScreenerLivePriceSource._decimal(volume.get("h24"))
                if isinstance(volume, dict)
                else None
            )
            if (
                liq_usd is not None
                and liq_usd < _DUST_LIQUIDITY_USD
                and mcap is not None
                and mcap < _DUST_MCAP_USD
            ):
                continue
            if liq_usd is not None and liq_usd > 0:
                score = liq_usd
            elif mcap is not None and mcap > 0:
                score = mcap
            elif vol24 is not None and vol24 > 0:
                score = vol24
            else:
                score = Decimal("0")
            price_native = DexScreenerLivePriceSource._decimal(pair.get("priceNative"))
            if price_native is not None and price_native <= 0:
                price_native = None
            if best is None or score > best[0]:
                best = (score, price, price_native)
        if best is None:
            return None
        _, price, price_native = best
        return PriceQuote(
            price=price,
            ts=datetime.now(UTC),
            source="dexscreener_live",
            price_native=price_native,
        )


class LiveFirstPriceSource:
    """Live quote first, persisted snapshot as fallback.

    Same ``price(mint, chain)`` interface as the other sources, so the
    trade engine and candidate builder use it unchanged. A live failure
    never blocks the trade path by itself -- the snapshot fallback still
    applies, and its own staleness is still enforced by the caller.
    """

    def __init__(
        self, live: DexScreenerLivePriceSource, fallback: MarketSnapshotPriceSource
    ) -> None:
        self.live = live
        self.fallback = fallback

    def price(self, mint: str, chain: str) -> PriceQuote | None:
        try:
            quote = self.live.price(mint, chain)
        except Exception:
            log.error("live_price_source_error", exc_info=True)
            quote = None
        if quote is not None:
            return quote
        return self.fallback.price(mint, chain)
