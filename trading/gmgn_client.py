"""Gmgn read-only client for price and market data."""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from common.config import get_settings
from common.http import HttpClient
from trading.executors import PriceQuote

log = logging.getLogger(__name__)

# Module-level flags
_gmgn_unconfigured_logged = False
_blocked_until: float = 0.0

# Cache: dict keyed (chain, mint) with TTL
_price_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_trending_cache: dict[tuple[str, str], tuple[float, Any]] = {}

# Rate limiter: thread-safe token bucket, max 2 requests/second
_rate_lock = threading.Lock()
_last_request_time: float = 0.0


def token_url(chain: str, address: str) -> str:
    """Return the gmgn token URL for a given chain and address.

    Mapping: solana->sol, base->base, ethereum->eth.
    Unknown chain raises ValueError.
    """
    slug_map = {"solana": "sol", "base": "base", "ethereum": "eth"}
    if chain not in slug_map:
        raise ValueError(f"Unknown chain: {chain}")
    slug = slug_map[chain]
    return f"https://gmgn.ai/{slug}/token/{address}"


def _is_unconfigured() -> bool:
    """Check if gmgn_api_base is empty settings."""
    return get_settings().gmgn_api_base == ""


def _log_unconfigured() -> None:
    """Log gmgn_unconfigured once per process."""
    global _gmgn_unconfigured_logged
    if not _gmgn_unconfigured_logged:
        _gmgn_unconfigured_logged = True
        log.info("gmgn_unconfigured")


def _note_error(exc: Exception) -> None:
    """Block for 30 s on HTTP 429; never leak the exception body."""
    global _blocked_until
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        _blocked_until = time.monotonic() + 30


def _rate_limit() -> None:
    """Thread-safe rate limiter: max 2 requests/second across the process."""
    global _last_request_time
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < 0.5:  # 2 req/s => min 0.5s between requests
            time.sleep(0.5 - elapsed)
        _last_request_time = time.monotonic()


def _cache_get_price(cache_dict, key):
    """Get from price cache if not expired (TTL 10 s)."""
    if key in cache_dict:
        ts, value = cache_dict[key]
        if time.monotonic() - ts < 10:
            return value
        else:
            del cache_dict[key]
    return None


def _cache_set_price(cache_dict, key, value) -> None:
    """Set price cache with timestamp."""
    cache_dict[key] = (time.monotonic(), value)


def _cache_get_trending(cache_dict, key):
    """Get from trending cache if not expired (TTL 60 s)."""
    if key in cache_dict:
        ts, value = cache_dict[key]
        if time.monotonic() - ts < 60:
            return value
        else:
            del cache_dict[key]
    return None


def _cache_set_trending(cache_dict, key, value) -> None:
    """Set trending cache with timestamp."""
    cache_dict[key] = (time.monotonic(), value)


class GmgnReadClient:
    """Read-only client for Gmgn.ai price and market data."""

    def __init__(self) -> None:
        self.http = HttpClient(base_url=get_settings().gmgn_api_base)

    def _check_unconfigured(self) -> bool:
        """Return True if gmgn is not configured, logging once if so."""
        if _is_unconfigured():
            _log_unconfigured()
            return True
        return False

    def _check_blocked(self) -> bool:
        """Check if we're blocked due to 429. Returns True if blocked."""
        global _blocked_until
        if _blocked_until > time.monotonic():
            return True
        return False

    def price(self, mint: str, chain: str) -> PriceQuote | None:
        """Fetch current price for a token.

        Returns PriceQuote with source="gmgn" and UTC ts, or None.
        """
        # 1. Check unconfigured
        if self._check_unconfigured():
            return None

        # 2. Check blocked (429) - must come before cache check
        if self._check_blocked():
            return None

        # 3. Check cache (TTL 10 s for price)
        cache_key = (chain, mint)
        cached = _cache_get_price(_price_cache, cache_key)
        if cached is not None:
            return cached

        # 4. Rate limit
        _rate_limit()

        # 5. Make HTTP request
        try:
            result = self.http.get_json("/token/price", params={"mint": mint, "chain": chain})
            # Build PriceQuote from result
            if not result or result.get("price") is None:
                return None
            price_value = Decimal(str(result["price"]))
            if price_value <= 0:
                return None
            quote = PriceQuote(
                price=price_value,
                ts=datetime.now(UTC),
                source="gmgn",
            )
            # Cache the result
            _cache_set_price(_price_cache, cache_key, quote)
            return quote
        except Exception as e:
            # Log gmgn_error with exception class name only
            log.error("gmgn_error", exc_info=True)
            global _blocked_until
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code == 429:
                _blocked_until = time.monotonic() + 30
            return None

    def market_cap(self, mint: str, chain: str) -> Decimal | None:
        """Fetch market cap for a token."""
        # 1. Check unconfigured
        if self._check_unconfigured():
            return None

        # 2. Check blocked (429) - must come before cache check
        if self._check_blocked():
            return None

        # 3. No cache for market cap (the price cache holds PriceQuote objects).

        # 4. Rate limit
        _rate_limit()

        # 5. Make HTTP request
        try:
            result = self.http.get_json("/token/market_cap", params={"mint": mint, "chain": chain})
            if result and "market_cap" in result:
                return Decimal(str(result["market_cap"]))
            return None
        except Exception as e:
            log.error("gmgn_error", exc_info=True)
            _note_error(e)
            return None

    def liquidity(self, mint: str, chain: str) -> Decimal | None:
        """Fetch liquidity for a token."""
        if self._check_unconfigured():
            return None

        if self._check_blocked():
            return None

        _rate_limit()

        try:
            result = self.http.get_json("/token/liquidity", params={"mint": mint, "chain": chain})
            if result and "liquidity" in result:
                return Decimal(str(result["liquidity"]))
            return None
        except Exception as e:
            log.error("gmgn_error", exc_info=True)
            _note_error(e)
            return None

    def holders(self, mint: str, chain: str) -> int | None:
        """Fetch holder count for a token."""
        if self._check_unconfigured():
            return None

        if self._check_blocked():
            return None

        _rate_limit()

        try:
            result = self.http.get_json("/token/holders", params={"mint": mint, "chain": chain})
            if result and "count" in result:
                return int(result["count"])
            return None
        except Exception as e:
            log.error("gmgn_error", exc_info=True)
            _note_error(e)
            return None

    def trending(self, chain: str, limit: int = 20) -> list[dict] | None:
        """Fetch trending tokens for a chain."""
        if self._check_unconfigured():
            return None

        if self._check_blocked():
            return None

        # Check cache (TTL 60 s for trending)
        cache_key = (chain, limit)
        cached = _cache_get_trending(_trending_cache, cache_key)
        if cached is not None:
            return cached

        _rate_limit()

        try:
            result = self.http.get_json("/token/trending", params={"chain": chain, "limit": limit})
            if result:
                return result
            return None
        except Exception as e:
            log.error("gmgn_error", exc_info=True)
            _note_error(e)
            return None
