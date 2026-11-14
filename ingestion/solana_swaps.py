"""Wallet-level Solana swap reconstruction from RPC pool transactions.

GeckoTerminal's pool-trades endpoint is the primary first-buy feed, but its
public rate limit can turn a fresh pool's feed into an empty list. This module
rebuilds the same swap shape from ``getSignaturesForAddress`` and
``getTransaction`` so tracked-wallet first buys still reach the copy queue.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from common.config import get_settings
from common.logging import get_logger
from common.urls import redact_url
from ingestion.provider_gate import CircuitOpen, ProviderGate

log = get_logger(__name__)

_MIN_TOKEN_DELTA = 1e-12
_rpc_gate = ProviderGate()


@dataclass
class _PoolSwapCache:
    created_at: float
    swaps: list[dict[str, Any]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)


_cache_lock = threading.Lock()
_swap_cache: dict[tuple[str, str], _PoolSwapCache] = {}


def reset_solana_swap_gate() -> None:
    _rpc_gate.reset()


def reset_solana_swap_cache() -> None:
    with _cache_lock:
        _swap_cache.clear()


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
    tracked: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch and normalize wallet swaps for a Solana pool, oldest first.

    Pacing and a 403/429 circuit keep a rate-limited RPC from stalling the
    whole scan. Once a tracked-wallet buy is reconstructed, the walk stops:
    that is the signal the copy queue needs.
    """
    settings = get_settings()
    _rpc_gate.configure(
        min_interval=settings.solana_swap_rpc_min_interval_seconds,
        cooldown=settings.provider_circuit_break_seconds,
    )
    cache_key = (pool_address, base_mint)
    now = time.monotonic()
    with _cache_lock:
        entry = _swap_cache.get(cache_key)
        if entry is None or now - entry.created_at > settings.first_buy_swap_cache_seconds:
            entry = _PoolSwapCache(created_at=now)
            _swap_cache[cache_key] = entry

    # Fetch a wider signature window than we parse per scan, then walk unseen
    # transactions oldest-first. Repeated scans continue where the last stopped.
    signature_limit = max(limit * 4, 24)
    try:
        _rpc_gate.before()
        signatures = rpc.get_signatures_for_address(pool_address, limit=signature_limit)
    except CircuitOpen:
        return []
    except Exception as exc:  # noqa: BLE001 - ingestion must survive an RPC outage.
        _rpc_gate.record_failure(exc)
        log.warning(
            "solana_swap_signatures_failed",
            pool=pool_address,
            error=redact_url(str(exc)),
        )
        return []

    unseen = [
        item
        for item in reversed(signatures)
        if isinstance(item, dict)
        and item.get("err") is None
        and isinstance(item.get("signature"), str)
        and item["signature"] not in entry.seen
    ]
    for item in unseen[: max(0, limit)]:
        signature = item["signature"]
        try:
            _rpc_gate.before()
            transaction = rpc.get_transaction(signature)
        except CircuitOpen:
            break
        except Exception as exc:  # noqa: BLE001 - skip one bad historical transaction.
            _rpc_gate.record_failure(exc)
            log.warning(
                "solana_swap_transaction_failed",
                pool=pool_address,
                signature=signature,
                error=redact_url(str(exc)),
            )
            if _rpc_gate.is_open():
                break
            continue
        tx_swaps = swaps_from_transaction(
            transaction,
            pool_address=pool_address,
            base_mint=base_mint,
            price_usd=price_usd,
        )
        entry.swaps.extend(tx_swaps)
        entry.seen.add(signature)
        if tracked and any(
            swap["side"] == "buy" and swap["wallet"] in tracked for swap in tx_swaps
        ):
            break
    return list(entry.swaps)
