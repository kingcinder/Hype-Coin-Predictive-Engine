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

    def __init__(self, api_key: str | None = None) -> None:
        headers: dict[str, str] = {}
        if api_key:
            headers["X-API-KEY"] = api_key
        self.http = HttpClient(base_url=self.BASE_URL, headers=headers)

    def close(self) -> None:
        self.http.close()

    def token_overview(self, address: str) -> dict[str, Any] | None:
        """Get token overview: price, volume, liquidity, holder count."""
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
        """Get newly created tokens on Solana."""
        data = self.http.get_json(
            "/defi/v3/token/new_listing",
            params={"limit": limit, "sort_by": "created_time", "sort_type": "desc"},
        )
        if isinstance(data, dict) and data.get("success"):
            items = data.get("data", {}).get("items") or []
            return items if isinstance(items, list) else []
        return []

    def trending_tokens(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get trending tokens by volume change."""
        data = self.http.get_json(
            "/defi/v3/token/trending",
            params={"limit": limit},
        )
        if isinstance(data, dict) and data.get("success"):
            items = data.get("data", {}).get("items") or []
            return items if isinstance(items, list) else []
        return []

    def top_gainers(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get top gaining tokens."""
        data = self.http.get_json(
            "/defi/v3/token/top_gainer",
            params={"limit": limit},
        )
        if isinstance(data, dict) and data.get("success"):
            items = data.get("data", {}).get("items") or []
            return items if isinstance(items, list) else []
        return []
