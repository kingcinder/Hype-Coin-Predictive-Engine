"""Whale Tracker Night Crawler — large on-chain movements and wallet intelligence.

Tracks whale wallets, large transfers, and unusual accumulation patterns
that often precede hype coin pumps. Uses public blockchain RPCs and
block explorer APIs to detect smart money movements.
"""
from __future__ import annotations

from typing import Any

from common.time import utc_now
from crawlers.base import BaseCrawler


class WhaleTrackerCrawler(BaseCrawler):
    """Tracks whale wallets and large on-chain movements."""

    def __init__(self) -> None:
        super().__init__(
            name="whale_tracker",
            max_retries=2,
            retry_delay_seconds=3.0,
            rate_limit_pause=1.0,
            timeout_seconds=15.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_eth_whales())
        items.extend(self._fetch_sol_whales())
        return items

    def _fetch_eth_whales(self) -> list[dict[str, Any]]:
        """Fetch recent large ETH/token transfers via Etherscan."""
        try:
            # Use public Etherscan API (no key needed for basic endpoints)
            data = self.client.get(
                "https://api.etherscan.io/api",
                params={
                    "module": "account",
                    "action": "txlist",
                    "address": "0x00000000219ab540356cBB839Cbe05303d7705Fa",  # ETH2 deposit
                    "startblock": 0,
                    "endblock": 99999999,
                    "page": 1,
                    "offset": 10,
                    "sort": "desc",
                },
            ).json()
            items = []
            for tx in (data.get("result") or [])[:10]:
                value_eth = int(tx.get("value", "0")) / 1e18
                if value_eth < 10:
                    continue
                items.append({
                    "title": f"Whale transfer: {value_eth:.2f} ETH",
                    "text": f"Large ETH movement of {value_eth:.2f} ETH from {tx.get('from', '')[:10]}...",
                    "url": f"https://etherscan.io/tx/{tx.get('hash', '')}",
                    "published": utc_now(),
                    "source_domain": "etherscan.io",
                    "source_type": "on_chain",
                    "metrics": {
                        "chain": "ethereum",
                        "tx_hash": tx.get("hash"),
                        "from": tx.get("from"),
                        "to": tx.get("to"),
                        "value_eth": value_eth,
                        "block": tx.get("blockNumber"),
                        "whale_alert": True,
                    },
                })
            return items
        except Exception:
            return []

    def _fetch_sol_whales(self) -> list[dict[str, Any]]:
        """Fetch recent large SOL transfers."""
        try:
            # Use Solana public RPC for recent large transactions
            data = self.client.post(
                "https://api.mainnet-beta.solana.com",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getRecentBlockhash",
                    "params": [],
                },
            ).json()
            # In production, this would analyze recent large transfers
            # For now, return empty as a placeholder for the whale tracking logic
            return []
        except Exception:
            return []
