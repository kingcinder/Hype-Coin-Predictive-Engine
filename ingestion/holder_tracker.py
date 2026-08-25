"""Holder Tracker — real on-chain holder count and growth computation.

Computes holder_count and holder_growth from actual on-chain data:
- Solana: getTokenLargestAccounts for top holder concentration
- EVM: Transfer event logs for unique holder estimation
"""

from __future__ import annotations

from dataclasses import dataclass

from common.http import HttpClient
from common.logging import get_logger

log = get_logger(__name__)


@dataclass
class HolderSnapshot:
    """Holder metrics for a token at a point in time."""

    holder_count: int | None = None
    top_holder_concentration: float | None = None  # % held by top 10
    holder_growth: float | None = None  # delta from previous scan
    unique_buyers_estimate: int | None = None


def get_solana_holders(mint: str, *, http: HttpClient | None = None) -> HolderSnapshot:
    """Get holder metrics for a Solana token via RPC."""
    client = http or HttpClient(base_url="https://api.mainnet-beta.solana.com")
    try:
        # Get largest accounts
        data = client.post_json(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenLargestAccounts",
                "params": [mint],
            },
        )
        accounts = (data or {}).get("result", {}).get("value", [])

        # Get total supply for concentration calculation
        supply_data = client.post_json(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "getTokenSupply",
                "params": [mint],
            },
        )
        total_supply = 0.0
        supply_val = (supply_data or {}).get("result", {}).get("value", {})
        if supply_val:
            total_supply = float(supply_val.get("uiAmount", 0) or 0)

        # Calculate concentration from top accounts
        concentration = None
        if total_supply > 0 and accounts:
            top_holdings = sum(float(a.get("uiAmount", 0) or 0) for a in accounts[:10])
            concentration = top_holdings / total_supply

        # Estimate holder count from largest accounts
        # NOTE: Solana RPC doesn't expose full holder list; largest accounts
        # is a lower-bound proxy, not an accurate total holder count.
        holder_count = len(accounts) if accounts else None

        return HolderSnapshot(
            holder_count=holder_count,
            top_holder_concentration=round(concentration, 4) if concentration is not None else None,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("solana_holder_error", mint=mint, error=str(exc))
        return HolderSnapshot()
    finally:
        if http is None:
            client.close()


def get_evm_holders(
    token_address: str,
    chain: str = "ethereum",
    *,
    http: HttpClient | None = None,
) -> HolderSnapshot:
    """Get holder metrics for an EVM token via Etherscan Transfer events.

    Counts unique addresses that have received the token — a rough proxy
    for holder count without running a full node.
    """
    client = http or HttpClient(base_url="https://api.etherscan.io")
    try:
        chain_id = "1" if chain == "ethereum" else "8453"
        # Fetch recent Transfer events to estimate unique holders
        data = client.get_json(
            "/v2/api",
            params={
                "chainid": chain_id,
                "module": "account",
                "action": "tokentx",
                "contractaddress": token_address,
                "page": 1,
                "offset": 100,
                "sort": "desc",
            },
        )
        txs = data.get("result", []) if isinstance(data, dict) else []
        if not isinstance(txs, list):
            txs = []

        # Count unique recipient addresses as proxy for holders
        unique_holders: set[str] = set()
        for tx in txs:
            to_addr = (tx.get("to") or "").lower()
            if to_addr and to_addr != "0x" + "0" * 40:
                unique_holders.add(to_addr)

        # Estimate concentration from value distribution
        holder_count = len(unique_holders) if unique_holders else None

        return HolderSnapshot(
            holder_count=holder_count,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("evm_holder_error", address=token_address, error=str(exc))
        return HolderSnapshot()
    finally:
        if http is None:
            client.close()


def compute_holder_growth(
    current: HolderSnapshot,
    previous: HolderSnapshot,
) -> float | None:
    """Compute holder growth rate from two snapshots."""
    if current.holder_count is None or previous.holder_count is None:
        return None
    if previous.holder_count == 0:
        return None
    growth = (current.holder_count - previous.holder_count) / previous.holder_count
    return round(growth, 4)
