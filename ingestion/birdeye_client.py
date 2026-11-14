"""Birdeye API client — Solana-focused market data as a third data source.

Provides price, volume, and token profile data as a fallback when
DexScreener/GeckoTerminal are rate-limited or down. Free tier available.
"""

from __future__ import annotations

from typing import Any

from common.http import HttpClient


class BirdeyeClient:
    """Birdeye API client for Solana market data."""

    BASE_URL = "https://public-api.birdeye.so"

    def __init__(self, api_key: str | None = None, *, chain: str = "solana") -> None:
        headers: dict[str, str] = {"x-chain": chain}
        if api_key:
            headers["X-API-KEY"] = api_key
        self.http = HttpClient(base_url=self.BASE_URL, headers=headers)

    def close(self) -> None:
        self.http.close()

    def token_overview(self, address: str) -> dict[str, Any] | None:
        """Get token overview: price, volume, liquidity, holder count.

        Audited against the live API (2026-11): ``/defi/token_overview`` is
        current and returns the token snapshot directly under ``data``.
        """
        data = self.http.get_json(
            "/defi/token_overview",
            params={"address": address},
        )
        if isinstance(data, dict) and data.get("success"):
            return data.get("data")
        return None

    def token_meta(self, address: str) -> dict[str, Any] | None:
        """Get token metadata: name, symbol, decimals, creator."""
        data = self.http.get_json(
            "/defi/token_meta",
            params={"address": address},
        )
        if isinstance(data, dict) and data.get("success"):
            return data.get("data")
        return None

    def new_tokens(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get newly created tokens on Solana, most recent first.

        Birdeye's new-listing endpoint is ``/defi/v2/tokens/new_listing`` (the
        old ``/defi/v3/token/new_listing`` path is gone and answers 404); the
        chain comes from the ``x-chain`` header and ordering is implicit.
        """
        data = self.http.get_json(
            "/defi/v2/tokens/new_listing",
            params={"limit": limit},
        )
        if isinstance(data, dict) and data.get("success"):
            items = data.get("data", {}).get("items") or []
            return items if isinstance(items, list) else []
        return []

    def trending_tokens(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get the ranked trending-token list (``data.tokens``).

        The old ``/defi/v3/token/trending`` path is gone (404). The live
        endpoint is ``/defi/token_trending``, capped at 20 tokens per page.
        """
        data = self.http.get_json(
            "/defi/token_trending",
            params={
                "sort_by": "rank",
                "sort_type": "asc",
                "offset": 0,
                "limit": min(max(limit, 1), 20),
            },
        )
        if isinstance(data, dict) and data.get("success"):
            payload = data.get("data")
            tokens = payload.get("tokens") if isinstance(payload, dict) else None
            return tokens if isinstance(tokens, list) else []
        return []

    def top_gainers(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get top gaining tokens by 24h price change (``data.items``).

        The old ``/defi/v3/token/top_gainer`` path is gone (404); the live
        screener endpoint is ``/defi/v3/token/list`` sorted by
        ``price_change_24h_percent``.
        """
        data = self.http.get_json(
            "/defi/v3/token/list",
            params={
                "sort_by": "price_change_24h_percent",
                "sort_type": "desc",
                "limit": min(max(limit, 1), 100),
            },
        )
        if isinstance(data, dict) and data.get("success"):
            payload = data.get("data")
            items = payload.get("items") if isinstance(payload, dict) else None
            return items if isinstance(items, list) else []
        return []
