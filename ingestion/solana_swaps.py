"""Wallet-level Solana swap reconstruction from RPC pool transactions.

GeckoTerminal's pool-trades endpoint is the primary first-buy feed, but its
public rate limit can turn a fresh pool's feed into an empty list. This module
rebuilds the same swap shape from ``getSignaturesForAddress`` and
``getTransaction`` so tracked-wallet first buys still reach the copy queue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.logging import get_logger

log = get_logger(__name__)

_MIN_TOKEN_DELTA = 1e-12


def _amount(entry: dict[str, Any]) -> float:
    amount = entry.get("uiTokenAmount") or {}
    raw = amount.get("uiAmount")
    if raw is None:
        raw = amount.get("uiAmountString")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def swaps_from_transaction(
    transaction: dict[str, Any] | None,
    *,
    pool_address: str,
    base_mint: str,
    price_usd: float | None = None,
) -> list[dict[str, Any]]:
    """Map one parsed Solana transaction to wallet buys/sells of ``base_mint``."""
    result = transaction.get("result") if isinstance(transaction, dict) else None
    if not isinstance(result, dict):
        return []
    meta = result.get("meta")
    block_time = result.get("blockTime")
    if not isinstance(meta, dict) or block_time is None:
        return []

    before: dict[str, float] = {}
    after: dict[str, float] = {}
    for key, target in (("preTokenBalances", before), ("postTokenBalances", after)):
        for entry in meta.get(key) or []:
            if not isinstance(entry, dict) or entry.get("mint") != base_mint:
                continue
            owner = entry.get("owner")
            if not isinstance(owner, str) or not owner or owner == pool_address:
                continue
            target[owner] = target.get(owner, 0.0) + _amount(entry)

    ts = datetime.fromtimestamp(float(block_time), tz=UTC)
    swaps: list[dict[str, Any]] = []
    for wallet in sorted(set(before) | set(after)):
        delta = after.get(wallet, 0.0) - before.get(wallet, 0.0)
        if abs(delta) < _MIN_TOKEN_DELTA:
            continue
        swap: dict[str, Any] = {
            "wallet": wallet,
            "side": "buy" if delta > 0 else "sell",
            "ts": ts,
        }
        if price_usd is not None:
            swap["size_usd"] = abs(delta) * price_usd
        swaps.append(swap)
    return swaps


def fetch_pool_swaps(
    rpc: Any,
    *,
    pool_address: str,
    base_mint: str,
    limit: int = 20,
    price_usd: float | None = None,
) -> list[dict[str, Any]]:
    """Fetch and normalize wallet swaps for a Solana pool, oldest first."""
    try:
        signatures = rpc.get_signatures_for_address(pool_address, limit=limit)
    except Exception as exc:  # noqa: BLE001 - ingestion must survive an RPC outage.
        log.warning("solana_swap_signatures_failed", pool=pool_address, error=str(exc))
        return []

    swaps: list[dict[str, Any]] = []
    for entry in reversed(signatures):
        if not isinstance(entry, dict) or entry.get("err") is not None:
            continue
        signature = entry.get("signature")
        if not isinstance(signature, str) or not signature:
            continue
        try:
            transaction = rpc.get_transaction(signature)
        except Exception as exc:  # noqa: BLE001 - skip one bad historical transaction.
            log.warning(
                "solana_swap_transaction_failed",
                pool=pool_address,
                signature=signature,
                error=str(exc),
            )
            continue
        swaps.extend(
            swaps_from_transaction(
                transaction,
                pool_address=pool_address,
                base_mint=base_mint,
                price_usd=price_usd,
            )
        )
    return swaps
