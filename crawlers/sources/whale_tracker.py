"""Whale Tracker Night Crawler — large on-chain movements and wallet intelligence.

Tracks whale wallets, large transfers, and unusual accumulation patterns
that often precede hype coin pumps. Uses public blockchain RPCs and
block explorer APIs to detect smart money movements.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from common.config import get_settings, secret_value
from common.logging import get_logger
from common.time import utc_now
from common.urls import redact_url
from crawlers.base import BaseCrawler

log = get_logger(__name__)

# Etherscan V2 requires the key on every endpoint; V1 is retired.
_ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
_ETHEREUM_CHAIN_ID = "1"


def _etherscan_params(**params: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"chainid": _ETHEREUM_CHAIN_ID, **params}
    api_key = secret_value(get_settings().etherscan_api_key)
    if api_key:
        out["apikey"] = api_key
    return out


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

UNISWAP_V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
ETH_WATCH_TARGETS: tuple[tuple[str, str | None], ...] = tuple(
    (address, None) for address in ETH_WHALE_ADDRESSES
) + ((UNISWAP_V2_ROUTER, "uniswap_v2"),)

SOL_WHALE_ACCOUNTS = [
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # Binance
    "5VCwKtCXgCJ6kit5FybXkhvErEB2g2f6J5LMkmbmSVr6",  # FTX (historical)
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiGPkr",  # OKX
]


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
        return [*self._fetch_eth_transfers(), *self._fetch_sol_whales()]

    # ── Ethereum whale tracking ──────────────────────────────────────────

    def _fetch_eth_transfers(self) -> list[dict[str, Any]]:
        """Fetch recent large ETH transfers for every watched Ethereum address."""
        if not secret_value(get_settings().etherscan_api_key):
            log.info(
                "whale_tracker_eth_skipped: ETHERSCAN_API_KEY is not set; "
                "Ethereum whale feed deferred"
            )
            return []
        items: list[dict[str, Any]] = []
        for address, dex in ETH_WATCH_TARGETS:
            try:
                items.extend(self._fetch_eth_address(address, dex=dex))
            except Exception as exc:  # noqa: BLE001
                log.debug("whale_eth_error", address=address, error=redact_url(str(exc)))
            time.sleep(0.25)  # Etherscan rate limit
        return items

    def _fetch_eth_address(self, address: str, *, dex: str | None) -> list[dict[str, Any]]:
        """Return whale-sized transfers for one address, optionally labelled as DEX flow."""
        # The old 5-tx window per whale address almost never contained a
        # >=50 ETH move (max seen in a batch: 2.0 ETH). Widen so the
        # threshold has a real chance to hit: 50 per wallet, 100 for the
        # high-traffic Uniswap router.
        limit = 100 if dex else 50
        data = self.client.get(
            _ETHERSCAN_V2_URL,
            params=_etherscan_params(
                module="account",
                action="txlist",
                address=address,
                startblock=0,
                offset=limit,
                sort="desc",
            ),
        ).json()
        transactions = data.get("result")
        if not isinstance(transactions, list):
            return []
        return [
            item
            for tx in transactions[:limit]
            if isinstance(tx, dict) and (item := _eth_transfer_item(tx, dex=dex)) is not None
        ]

    # ── Solana whale tracking ────────────────────────────────────────────

    def _fetch_sol_whales(self) -> list[dict[str, Any]]:
        """Fetch recent large SOL transfers via Solana RPC.

        Uses getSignaturesForAddress on known whale wallets, then fetches
        transaction details for large transfers.
        """
        items: list[dict[str, Any]] = []
        # Helius when the key is set (fast, authenticated); the public RPC is
        # slow (~19s for a 5-signature probe) and rate-limited. Widen the
        # window too: 25 signatures in, 10 tx details each (was 10/5).
        helius_key = secret_value(get_settings().helius_api_key)
        sol_rpc_url = (
            f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
            if helius_key
            else "https://api.mainnet-beta.solana.com"
        )
        for account in SOL_WHALE_ACCOUNTS:  # Limit to avoid rate limits
            try:
                sig_data = self.client.post(
                    sol_rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getSignaturesForAddress",
                        "params": [account, {"limit": 25}],
                    },
                ).json()
                signatures = sig_data.get("result") or []
                for sig_info in signatures[:10]:
                    sig = sig_info.get("signature")
                    if not sig:
                        continue
                    # Fetch transaction details
                    tx_data = self.client.post(
                        sol_rpc_url,
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
                        items.append(
                            {
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
                            }
                        )
                    time.sleep(0.1)  # Rate limit
            except Exception as exc:  # noqa: BLE001
                log.debug("whale_sol_error", account=account, error=redact_url(str(exc)))
        return items


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


def _eth_transfer_item(tx: dict[str, Any], *, dex: str | None) -> dict[str, Any] | None:
    """Map one Etherscan tx to a whale item, or None below the ETH threshold."""
    value_eth = int(tx.get("value", "0")) / 1e18
    if value_eth < ETH_MIN_VALUE_ETH:
        return None
    tx_from = tx.get("from") or ""
    tx_to = tx.get("to") or ""
    if dex:
        title = f"Large DEX swap: {value_eth:,.1f} ETH via Uniswap"
        text = (
            f"Whale-sized swap of {value_eth:,.1f} ETH detected on "
            f"Uniswap V2 Router from {tx_from[:10]}..."
        )
    else:
        title = f"Whale transfer: {value_eth:,.1f} ETH"
        text = (
            f"Large ETH movement of {value_eth:,.1f} ETH from {tx_from[:10]}... to {tx_to[:10]}..."
        )
    metrics: dict[str, Any] = {
        "chain": "ethereum",
        "tx_hash": tx.get("hash"),
        "from": tx_from,
        "to": tx_to,
        "value_eth": round(value_eth, 4),
        "block": tx.get("blockNumber"),
        "whale_alert": True,
    }
    if dex:
        metrics["dex"] = dex
    return {
        "title": title,
        "text": text,
        "url": f"https://etherscan.io/tx/{tx.get('hash', '')}",
        "published": _unix_to_iso(int(tx.get("timeStamp", 0))),
        "source_domain": "etherscan.io",
        "source_type": "on_chain",
        "metrics": metrics,
    }


def _unix_to_iso(ts: int) -> datetime:
    """Convert a Unix timestamp to UTC, falling back to now for missing values."""
    return datetime.fromtimestamp(ts, tz=UTC) if ts > 0 else utc_now()
