"""Whale Tracker Night Crawler — large on-chain movements and wallet intelligence.

Tracks whale wallets, large transfers, and unusual accumulation patterns
that often precede hype coin pumps. Uses public blockchain RPCs and
block explorer APIs to detect smart money movements.
"""
from __future__ import annotations

import time
from datetime import UTC
from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler

log = get_logger(__name__)

# Known whale / smart-money addresses to monitor
ETH_WHALE_ADDRESSES = [
    "0x00000000219ab540356cBB839Cbe05303d7705Fa",  # ETH2 deposit contract
    "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8",  # Binance cold wallet
    "0xF977814e90dA44bFA03b6295A0616a897441aceC",  # Binance hot wallet
    "0x28C6c06298d514Db089934071355E5743bf21d60",  # Binance 14
    "0x47ac0Fb4F2D84898e4D9E7b4DaB3C24507a6D503",  # Justin Sun
]



# Minimum transfer values to flag as whale activity
ETH_MIN_VALUE_ETH = 50.0
SOL_MIN_VALUE_SOL = 1000.0


class WhaleTrackerCrawler(BaseCrawler):
    """Tracks whale wallets and large on-chain movements."""

    def __init__(self) -> None:
        super().__init__(
            name="whale_tracker",
            max_retries=2,
            retry_delay_seconds=3.0,
            rate_limit_pause=1.5,
            timeout_seconds=20.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_eth_whales())
        items.extend(self._fetch_eth_large_swaps())
        items.extend(self._fetch_sol_whales())
        return items

    # ── Ethereum whale tracking ──────────────────────────────────────────

    def _fetch_eth_whales(self) -> list[dict[str, Any]]:
        """Fetch recent large ETH transfers from known whale addresses."""
        items: list[dict[str, Any]] = []
        for address in ETH_WHALE_ADDRESSES:
            try:
                data = self.client.get(
                    "https://api.etherscan.io/api",
                    params={
                        "module": "account",
                        "action": "txlist",
                        "address": address,
                        "startblock": 0,
                        "endblock": 99999999,
                        "page": 1,
                        "offset": 5,
                        "sort": "desc",
                    },
                ).json()
                for tx in (data.get("result") or [])[:5]:
                    value_eth = int(tx.get("value", "0")) / 1e18
                    if value_eth < ETH_MIN_VALUE_ETH:
                        continue
                    ts = _unix_to_iso(int(tx.get("timeStamp", 0)))
                    items.append({
                        "title": f"Whale transfer: {value_eth:,.1f} ETH",
                        "text": (
                            f"Large ETH movement of {value_eth:,.1f} ETH "
                            f"from {address[:10]}... to {(tx.get('to') or '')[:10]}..."
                        ),
                        "url": f"https://etherscan.io/tx/{tx.get('hash', '')}",
                        "published": ts,
                        "source_domain": "etherscan.io",
                        "source_type": "on_chain",
                        "metrics": {
                            "chain": "ethereum",
                            "tx_hash": tx.get("hash"),
                            "from": address,
                            "to": tx.get("to"),
                            "value_eth": round(value_eth, 4),
                            "block": tx.get("blockNumber"),
                            "whale_alert": True,
                        },
                    })
                time.sleep(0.25)  # Etherscan rate limit
            except Exception as exc:  # noqa: BLE001
                log.debug("whale_eth_error", address=address, error=str(exc))
        return items

    def _fetch_eth_large_swaps(self) -> list[dict[str, Any]]:
        """Detect large Uniswap V2/V3 swaps via Etherscan event logs.

        Looks for recent Swap events on major DEX routers where the ETH value
        exceeds the whale threshold.
        """
        items: list[dict[str, Any]] = []
        # Uniswap V2 Router
        router = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
        try:
            # Get recent transactions to the Uniswap router
            data = self.client.get(
                "https://api.etherscan.io/api",
                params={
                    "module": "account",
                    "action": "txlist",
                    "address": router,
                    "startblock": 0,
                    "endblock": 99999999,
                    "page": 1,
                    "offset": 20,
                    "sort": "desc",
                },
            ).json()
            for tx in (data.get("result") or [])[:20]:
                value_eth = int(tx.get("value", "0")) / 1e18
                if value_eth < ETH_MIN_VALUE_ETH:
                    continue
                ts = _unix_to_iso(int(tx.get("timeStamp", 0)))
                items.append({
                    "title": f"Large DEX swap: {value_eth:,.1f} ETH via Uniswap",
                    "text": (
                        f"Whale-sized swap of {value_eth:,.1f} ETH detected on "
                        f"Uniswap V2 Router from {(tx.get('from') or '')[:10]}..."
                    ),
                    "url": f"https://etherscan.io/tx/{tx.get('hash', '')}",
                    "published": ts,
                    "source_domain": "etherscan.io",
                    "source_type": "on_chain",
                    "metrics": {
                        "chain": "ethereum",
                        "tx_hash": tx.get("hash"),
                        "from": tx.get("from"),
                        "to": tx.get("to"),
                        "value_eth": round(value_eth, 4),
                        "block": tx.get("blockNumber"),
                        "dex": "uniswap_v2",
                        "whale_alert": True,
                    },
                })
        except Exception as exc:  # noqa: BLE001
            log.debug("whale_eth_swap_error", error=str(exc))
        return items

    # ── Solana whale tracking ────────────────────────────────────────────

    def _fetch_sol_whales(self) -> list[dict[str, Any]]:
        """Fetch recent large SOL transfers via Solana RPC.

        Uses getSignaturesForAddress on known whale wallets, then fetches
        transaction details for large transfers.
        """
        items: list[dict[str, Any]] = []
        # Use public Solana RPC for recent signatures on major accounts
        whale_accounts = _get_solana_whale_accounts()
        for account in whale_accounts[:3]:  # Limit to avoid rate limits
            try:
                sig_data = self.client.post(
                    "https://api.mainnet-beta.solana.com",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getSignaturesForAddress",
                        "params": [account, {"limit": 10}],
                    },
                ).json()
                signatures = sig_data.get("result") or []
                for sig_info in signatures[:5]:
                    sig = sig_info.get("signature")
                    if not sig:
                        continue
                    # Fetch transaction details
                    tx_data = self.client.post(
                        "https://api.mainnet-beta.solana.com",
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "getTransaction",
                            "params": [sig, {"encoding": "jsonParsed"}],
                        },
                    ).json()
                    tx = tx_data.get("result")
                    if not tx:
                        continue
                    sol_change = _extract_sol_transfer(tx, account)
                    if sol_change is not None and abs(sol_change) >= SOL_MIN_VALUE_SOL:
                        ts = _unix_to_iso(tx.get("blockTime", 0))
                        direction = "outflow" if sol_change < 0 else "inflow"
                        items.append({
                            "title": f"Solana whale {direction}: {abs(sol_change):,.0f} SOL",
                            "text": (
                                f"Large SOL {'sent' if sol_change < 0 else 'received'} by "
                                f"{account[:8]}...: {abs(sol_change):,.0f} SOL"
                            ),
                            "url": f"https://solscan.io/tx/{sig}",
                            "published": ts,
                            "source_domain": "solscan.io",
                            "source_type": "on_chain",
                            "metrics": {
                                "chain": "solana",
                                "tx_hash": sig,
                                "from": account if sol_change < 0 else None,
                                "to": account if sol_change > 0 else None,
                                "value_sol": round(abs(sol_change), 4),
                                "block": tx.get("slot"),
                                "whale_alert": True,
                            },
                        })
                    time.sleep(0.1)  # Rate limit
            except Exception as exc:  # noqa: BLE001
                log.debug("whale_sol_error", account=account, error=str(exc))
        return items


def _get_solana_whale_accounts() -> list[str]:
    """Return known Solana whale / exchange deposit addresses."""
    return [
        "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # Binance
        "5VCwKtCXgCJ6kit5FybXkhvErEB2g2f6J5LMkmbmSVr6",  # FTX (historical)
        "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiGPkr",  # OKX
    ]


def _extract_sol_transfer(tx: dict[str, Any], watch_account: str) -> float | None:
    """Extract net SOL balance change for the watched account from a parsed transaction."""
    try:
        meta = tx.get("meta", {})
        account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        # Find our account index
        account_idx = None
        for i, key in enumerate(account_keys):
            pubkey = key if isinstance(key, str) else key.get("pubkey", "")
            if pubkey == watch_account:
                account_idx = i
                break
        if account_idx is None:
            return None
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        if account_idx < len(pre_balances) and account_idx < len(post_balances):
            change_lamports = post_balances[account_idx] - pre_balances[account_idx]
            return change_lamports / 1e9  # lamports to SOL
    except Exception:  # noqa: BLE001
        pass
    return None


def _unix_to_iso(ts: int) -> Any:
    """Convert unix timestamp to ISO format string."""
    from datetime import datetime
    if ts <= 0:
        return utc_now()
    return datetime.fromtimestamp(ts, tz=UTC)
