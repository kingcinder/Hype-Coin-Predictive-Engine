"""EVM first-buy swaps from public RPC Swap logs.

When GeckoTerminal rate-limits, its pool-trades endpoint stops answering and
EVM first-buys are missed entirely: the on-chain fallback in
IngestionService._first_buy_swaps only covered Solana. This module rebuilds
the same wallet-swap shape from a free/public EVM RPC by reading the pair's
Uniswap V2/V3 Swap events with eth_getLogs and resolving each trade's
wallet, side, size and timestamp from the chain.

The swap dicts match normalize_geckoterminal_trades exactly so
radar.first_buys.record_first_buys consumes them unchanged:
{"wallet": str, "side": "buy" | "sell", "size_usd": float | None, "ts": datetime}

Two callers use it:

* the live first-buy path, when a GeckoTerminal trades fetch fails for a
  Base/Ethereum pool (the fallback);
* the catch-up pass, which re-checks EVM pairs whose 5-minute window closed
  before they were observed. The trades endpoint cannot look back, but the
  chain retains the full history, so the window is rebuilt from Swap logs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from eth_utils import keccak

from common.config import get_settings
from common.http import HttpClient
from common.logging import get_logger
from common.time import ensure_utc
from common.urls import redact_url
from ingestion.source_clients import get_rpc_url, rpc_pool_for

log = get_logger(__name__)

EVM_CHAINS = ("base", "ethereum")
# Average block time per chain (seconds). Only used to map a timestamp window
# onto a block range for eth_getLogs; the range is padded, so small drift is fine.
BLOCK_TIME_SECONDS = {"base": 2.0, "ethereum": 12.0}
# Uniswap V2: Swap(address,uint256,uint256,uint256,uint256,address)
SWAP_V2_TOPIC = "0x" + keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
# Uniswap V3: Swap(address,address,int256,int256,uint160,uint128,int24)
SWAP_V3_TOPIC = (
    "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
)
# decimals() selector.
DECIMALS_SELECTOR = "0x313ce567"
# Bound the decode work per call: a new pair rarely has more than a handful of
# swaps in its first 5 minutes, and the caller caps the returned swaps anyway.
_MAX_DECODED_LOGS = 64
# Pad the block range so block-time variance cannot clip a trade at the edge.
_BLOCK_RANGE_PADDING = 30
# Cap the in-memory decimals cache so a long-lived process cannot grow it unboundedly.
_DECIMALS_CACHE_MAX = 256
_decimals_cache: dict[str, int] = {}


def is_evm_address(value: str | None) -> bool:
    """Whether a value is shaped like a 20-byte EVM address.

    DexScreener stores Uniswap V4 poolIds (32-byte hashes, 66 chars) in
    Pool.address and Solana pools carry base58 addresses; neither is
    queryable via eth_getLogs and every endpoint rejects them with
    "invalid params". Length + 0x prefix is the bar (not strict hex: the
    test suite seeds placeholder addresses with non-hex characters).
    """
    return isinstance(value, str) and len(value) == 42 and value.startswith("0x")


def _hex_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _parse_u256(word: str) -> int | None:
    if not isinstance(word, str):
        return None
    try:
        return int(word, 16)
    except ValueError:
        return None


def _parse_i256(word: str) -> int | None:
    value = _parse_u256(word)
    if value is None:
        return None
    if value >= 1 << 255:
        value -= 1 << 256
    return value


def _topic_address(topic: Any) -> str | None:
    """An indexed address topic is a 32-byte word, right-aligned."""
    if not isinstance(topic, str) or len(topic) != 66 or not topic.startswith("0x"):
        return None
    return "0x" + topic[-40:]


def _rpc_call(http: HttpClient, method: str, params: list[Any], *, call_id: int) -> Any:
    """One JSON-RPC call, raising on an RPC-level error body.

    Public endpoints answer HTTP 200 with an error object for failures
    (bad range, unknown block, rate limit), so a bare result lookup would
    silently return nothing. Raising here lets the caller fail over to the
    next endpoint instead of mistaking an error for an empty log set.
    """
    payload = http.post_json(
        "/",
        json={"jsonrpc": "2.0", "id": call_id, "method": method, "params": params},
    )
    if isinstance(payload, dict) and payload.get("error") is not None:
        raise RuntimeError(f"{method} failed: {payload.get('error')}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} returned a non-object response")
    return payload.get("result")


def _block_range(
    http: HttpClient,
    *,
    chain_slug: str,
    window_start: datetime,
    window_end: datetime,
    max_blocks: int,
) -> tuple[int, int] | None:
    """Block range covering [window_start, window_end], or None if it is in the future."""
    latest = _hex_int(_rpc_call(http, "eth_blockNumber", [], call_id=1))
    if latest is None:
        raise RuntimeError("invalid eth_blockNumber response")
    latest_block = _rpc_call(http, "eth_getBlockByNumber", [hex(latest), False], call_id=2)
    latest_ts = _hex_int(latest_block.get("timestamp")) if isinstance(latest_block, dict) else None
    if latest_ts is None:
        raise RuntimeError("invalid eth_getBlockByNumber response")
    latest_dt = datetime.fromtimestamp(latest_ts, tz=UTC)
    block_time = BLOCK_TIME_SECONDS.get(chain_slug, 12.0)
    start_offset = (latest_dt - ensure_utc(window_start)).total_seconds() / block_time
    end_offset = (latest_dt - ensure_utc(window_end)).total_seconds() / block_time
    from_block = int(latest - start_offset) - _BLOCK_RANGE_PADDING
    to_block = int(latest - end_offset) + _BLOCK_RANGE_PADDING
    from_block = max(0, from_block)
    to_block = min(latest, to_block)
    if to_block - from_block > max_blocks:
        from_block = max(0, to_block - max_blocks)
    if from_block > to_block:
        return None
    return from_block, to_block


def _token_decimals(http: HttpClient, token_address: str) -> int | None:
    """ERC-20 decimals for a token, cached per address. None when unavailable."""
    key = token_address.lower()
    cached = _decimals_cache.get(key)
    if cached is not None:
        return cached
    try:
        result = _rpc_call(
            http,
            "eth_call",
            [{"to": token_address, "data": DECIMALS_SELECTOR}, "latest"],
            call_id=3,
        )
    except Exception:
        return None
    value = _hex_int(result)
    if value is None or not 0 <= value <= 36:
        return None
    if len(_decimals_cache) >= _DECIMALS_CACHE_MAX:
        _decimals_cache.clear()
    _decimals_cache[key] = value
    return value


def _block_timestamps(http: HttpClient, block_numbers: set[int]) -> dict[int, datetime]:
    """Block number -> timestamp, one eth_getBlockByNumber per distinct block."""
    timestamps: dict[int, datetime] = {}
    call_id = 10
    for number in sorted(block_numbers):
        try:
            block = _rpc_call(http, "eth_getBlockByNumber", [hex(number), False], call_id=call_id)
        except Exception:
            continue
        finally:
            call_id += 1
        ts = _hex_int(block.get("timestamp")) if isinstance(block, dict) else None
        if ts is not None:
            timestamps[number] = datetime.fromtimestamp(ts, tz=UTC)
    return timestamps


def _tx_sender(http: HttpClient, tx_hash: str) -> str | None:
    """The EOA that sent a transaction (the actual buyer for a routed swap)."""
    try:
        tx = _rpc_call(http, "eth_getTransactionByHash", [tx_hash], call_id=20)
    except Exception:
        return None
    sender = tx.get("from") if isinstance(tx, dict) else None
    return str(sender).lower() if isinstance(sender, str) and sender else None


def _decode_swap_log(
    log_item: dict[str, Any], *, base_is_token0: bool
) -> tuple[str, int, str | None] | None:
    """Decode one Swap log into (side, base_amount, wallet_to).

    base_amount is the base-token quantity that changed hands (out for a
    buy, in for a sell), in the token's smallest unit. Returns None when the
    log is not a clean V2/V3 buy or sell.
    """
    topics = log_item.get("topics")
    if not isinstance(topics, list) or not topics:
        return None
    signature = str(topics[0]).lower()
    data = str(log_item.get("data") or "")
    wallet_to = _topic_address(topics[2]) if len(topics) >= 3 else None
    if signature == SWAP_V2_TOPIC.lower():
        # data: amount0In, amount1In, amount0Out, amount1Out (4 x 32 bytes).
        if len(data) < 2 + 64 * 4:
            return None
        words = [data[2 + 64 * i : 2 + 64 * (i + 1)] for i in range(4)]
        amounts = [_parse_u256("0x" + word) for word in words]
        if any(amount is None for amount in amounts):
            return None
        amount0_in, amount1_in, amount0_out, amount1_out = amounts  # type: ignore[misc]
        if base_is_token0:
            if amount0_out > 0 and amount1_in > 0:
                return "buy", amount0_out, wallet_to
            if amount0_in > 0 and amount1_out > 0:
                return "sell", amount0_in, wallet_to
        else:
            if amount1_out > 0 and amount0_in > 0:
                return "buy", amount1_out, wallet_to
            if amount1_in > 0 and amount0_out > 0:
                return "sell", amount1_in, wallet_to
        return None
    if signature == SWAP_V3_TOPIC.lower():
        # data: amount0, amount1 (int256), sqrtPriceX96, liquidity, tick.
        # V3 amounts are signed: negative is token out to the trader.
        if len(data) < 2 + 64 * 2:
            return None
        words = [data[2 + 64 * i : 2 + 64 * (i + 1)] for i in range(2)]
        amount0 = _parse_i256("0x" + words[0])
        amount1 = _parse_i256("0x" + words[1])
        if amount0 is None or amount1 is None:
            return None
        if base_is_token0:
            if amount0 < 0 and amount1 > 0:
                return "buy", -amount0, wallet_to
            if amount0 > 0 and amount1 < 0:
                return "sell", amount0, wallet_to
        else:
            if amount1 < 0 and amount0 > 0:
                return "buy", -amount1, wallet_to
            if amount1 > 0 and amount0 < 0:
                return "sell", amount1, wallet_to
        return None
    return None


def _evm_swaps_from_endpoint(
    rpc_url: str,
    *,
    chain_slug: str,
    pool_address: str,
    base_address: str,
    quote_address: str,
    pair_created_at: datetime,
    window_minutes: int,
    limit: int,
    price_usd: float | None,
) -> list[dict[str, Any]]:
    """Swap logs for one pool from a single RPC endpoint, oldest first."""
    settings = get_settings()
    window_start = ensure_utc(pair_created_at)
    window_end = window_start + timedelta(minutes=window_minutes)
    base_is_token0 = base_address.lower() < quote_address.lower()
    with HttpClient(base_url=rpc_url) as http:
        block_range = _block_range(
            http,
            chain_slug=chain_slug,
            window_start=window_start,
            window_end=window_end,
            max_blocks=settings.first_buy_evm_rpc_max_blocks,
        )
        if block_range is None:
            return []
        from_block, to_block = block_range
        logs = _rpc_call(
            http,
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "address": pool_address,
                    "topics": [[SWAP_V2_TOPIC, SWAP_V3_TOPIC]],
                }
            ],
            call_id=4,
        )
        if not isinstance(logs, list):
            raise RuntimeError("invalid eth_getLogs response")
        # Oldest first (block number, then log index), bounded decode work.
        logs = sorted(
            logs,
            key=lambda item: (
                _hex_int(item.get("blockNumber")) or 0,
                _hex_int(item.get("logIndex")) or 0,
            ),
        )[:_MAX_DECODED_LOGS]
        decoded: list[tuple[dict[str, Any], str, int, str | None, int]] = []
        block_numbers: set[int] = set()
        for item in logs:
            if not isinstance(item, dict):
                continue
            block_number = _hex_int(item.get("blockNumber"))
            if block_number is None:
                continue
            parsed = _decode_swap_log(item, base_is_token0=base_is_token0)
            if parsed is None:
                continue
            side, base_amount, wallet_to = parsed
            decoded.append((item, side, base_amount, wallet_to, block_number))
            block_numbers.add(block_number)
        if not decoded:
            return []
        timestamps = _block_timestamps(http, block_numbers)
        in_window = [
            (item, side, base_amount, wallet_to, block_number)
            for item, side, base_amount, wallet_to, block_number in decoded
            if (ts := timestamps.get(block_number)) is not None and window_start <= ts <= window_end
        ]
        if not in_window:
            return []
        # Oldest first, capped at the caller's limit.
        in_window.sort(
            key=lambda entry: (
                _hex_int(entry[0].get("blockNumber")) or 0,
                _hex_int(entry[0].get("logIndex")) or 0,
            )
        )
        in_window = in_window[: max(0, limit)]
        decimals = _token_decimals(http, base_address) if price_usd is not None else None
        swaps: list[dict[str, Any]] = []
        for item, side, base_amount, wallet_to, block_number in in_window:
            ts = timestamps.get(block_number)
            if ts is None:
                continue
            tx_hash = str(item.get("transactionHash") or "")
            wallet = _tx_sender(http, tx_hash) if tx_hash else None
            if not wallet:
                wallet = wallet_to
            if not wallet:
                continue
            size_usd = None
            if price_usd is not None and decimals is not None and base_amount:
                size_usd = (base_amount / (10**decimals)) * float(price_usd)
            swaps.append({"wallet": wallet.lower(), "side": side, "size_usd": size_usd, "ts": ts})
        return swaps


def fetch_evm_pool_swaps(
    *,
    chain_slug: str,
    pool_address: str,
    base_address: str,
    quote_address: str | None,
    pair_created_at: datetime,
    window_minutes: int = 5,
    limit: int = 8,
    price_usd: float | None = None,
) -> list[dict[str, Any]]:
    """First-buy swaps for an EVM pool from a public RPC, oldest first.

    Fails over across the chain's health-scored RPC pool (up to 3 endpoints).
    Raises on RPC failure so the caller can log it and treat the window as
    unknown, never as an observed empty feed. Returns [] only when the inputs
    are not an EVM pool or the window is in the future.
    """
    if chain_slug not in EVM_CHAINS:
        return []
    if not is_evm_address(pool_address) or not is_evm_address(base_address):
        return []
    if quote_address is None or not is_evm_address(quote_address):
        return []
    primary = get_rpc_url(chain_slug)
    if not primary:
        raise RuntimeError(f"no RPC URL for {chain_slug}")
    pool = rpc_pool_for(chain_slug)
    candidates = [primary]
    if pool is not None:
        for state in sorted(pool.snapshot(), key=lambda item: (item.down, -item.health)):
            if state.url not in candidates:
                candidates.append(state.url)
    last_error: Exception | None = None
    for url in candidates[:3]:
        try:
            if pool is not None:
                pool.record_request(url)
            swaps = _evm_swaps_from_endpoint(
                url,
                chain_slug=chain_slug,
                pool_address=pool_address,
                base_address=base_address,
                quote_address=quote_address,
                pair_created_at=pair_created_at,
                window_minutes=window_minutes,
                limit=limit,
                price_usd=price_usd,
            )
            if pool is not None:
                pool.mark_success(url)
            return swaps
        except Exception as exc:  # noqa: BLE001 - fail over to the next endpoint.
            last_error = exc
            if pool is not None:
                pool.mark_failure(url)
            log.warning(
                "evm_pool_swaps_endpoint_failed",
                chain=chain_slug,
                url=redact_url(url),
                error=redact_url(str(exc)),
            )
    if last_error is None:
        raise RuntimeError(f"no RPC endpoint available for {chain_slug}")
    raise last_error
