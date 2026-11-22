"""EVM first-buy swaps from public RPC Swap logs.

Covers the Uniswap V2/V3 Swap decoding, the block-range mapping, the
endpoint failover, and the fetch_evm_pool_swaps entry point. All RPC
traffic goes through a fake JSON-RPC transport -- no live RPC calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import ingestion.evm_swaps as evm_swaps
from ingestion.evm_swaps import (
    SWAP_V2_TOPIC,
    SWAP_V3_TOPIC,
    _block_range,
    _decode_swap_log,
    fetch_evm_pool_swaps,
    is_evm_address,
)

POOL = "0x" + "33" * 20
BASE_TOKEN = "0x" + "00" * 19 + "01"
QUOTE_TOKEN = "0x4200000000000000000000000000000000000006"  # WETH on Base
BUYER = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
CREATED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
LATEST_BLOCK = 1_000_000
LATEST_TS = datetime(2026, 5, 1, 12, 4, tzinfo=UTC)
SWAP_BLOCK = 999_990
SWAP_TS = datetime(2026, 5, 1, 12, 2, tzinfo=UTC)


def _u256(value: int) -> str:
    return f"{value & ((1 << 256) - 1):064x}"


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def _v2_log(
    *,
    block: int = SWAP_BLOCK,
    index: int = 0,
    amount0_in: int = 0,
    amount1_in: int = 0,
    amount0_out: int = 0,
    amount1_out: int = 0,
    to: str = ROUTER,
    tx: str = "0x" + "aa" * 32,
) -> dict[str, Any]:
    data = "0x" + "".join(_u256(v) for v in [amount0_in, amount1_in, amount0_out, amount1_out])
    return {
        "address": POOL,
        "blockNumber": hex(block),
        "logIndex": hex(index),
        "transactionHash": tx,
        "topics": [SWAP_V2_TOPIC, _topic(ROUTER), _topic(to)],
        "data": data,
    }


def _v3_log(
    *,
    block: int = SWAP_BLOCK,
    index: int = 0,
    amount0: int = 0,
    amount1: int = 0,
    to: str = ROUTER,
    tx: str = "0x" + "bb" * 32,
) -> dict[str, Any]:
    # data: amount0, amount1, sqrtPriceX96, liquidity, tick.
    data = "0x" + "".join(_u256(v) for v in [amount0, amount1, 0, 0, 0])
    return {
        "address": POOL,
        "blockNumber": hex(block),
        "logIndex": hex(index),
        "transactionHash": tx,
        "topics": [SWAP_V3_TOPIC, _topic(ROUTER), _topic(to)],
        "data": data,
    }


class FakeHttp:
    """A canned JSON-RPC transport. ``responses`` maps method -> handler(params)."""

    def __init__(self, *, base_url: str = "", responses: dict | None = None):
        self.base_url = base_url
        self.responses = responses or {}
        self.calls: list[tuple[str, list]] = []

    def __enter__(self) -> FakeHttp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post_json(self, path: str, *, json: dict | None = None, headers: Any = None) -> Any:
        method = (json or {}).get("method")
        params = (json or {}).get("params") or []
        self.calls.append((method, params))
        handler = self.responses.get(method)
        if handler is None:
            raise RuntimeError(f"unexpected RPC method {method}")
        return handler(params)


def _rpc_responses(
    *,
    logs: list[dict[str, Any]],
    tx_from: str = BUYER,
    decimals: int = 18,
) -> dict[str, Any]:
    def block_number(params: list) -> dict:
        return {"result": hex(LATEST_BLOCK)}

    def block_by_number(params: list) -> dict:
        number = params[0]
        if number == hex(LATEST_BLOCK):
            ts = LATEST_TS
        elif number == hex(SWAP_BLOCK):
            ts = SWAP_TS
        else:
            ts = LATEST_TS - timedelta(seconds=(LATEST_BLOCK - int(number, 16)) * 2)
        return {"result": {"number": number, "timestamp": hex(int(ts.timestamp()))}}

    def get_logs(params: list) -> dict:
        return {"result": logs}

    def get_tx(params: list) -> dict:
        return {"result": {"from": tx_from}}

    def call(params: list) -> dict:
        return {"result": hex(decimals)}

    return {
        "eth_blockNumber": block_number,
        "eth_getBlockByNumber": block_by_number,
        "eth_getLogs": get_logs,
        "eth_getTransactionByHash": get_tx,
        "eth_call": call,
    }


def _patch_rpc(monkeypatch: pytest.MonkeyPatch, *, responses: dict, pool: Any = None) -> FakeHttp:
    fake = FakeHttp(responses=responses)
    monkeypatch.setattr(evm_swaps, "HttpClient", lambda **kwargs: fake)
    monkeypatch.setattr(evm_swaps, "get_rpc_url", lambda chain: "https://base.example")
    monkeypatch.setattr(evm_swaps, "rpc_pool_for", lambda chain: pool)
    return fake


def test_is_evm_address() -> None:
    assert is_evm_address(POOL)
    assert not is_evm_address("PoolSol1")
    assert not is_evm_address(None)
    # A 66-char V4 poolId is not an EVM address.
    assert not is_evm_address("0x" + "ab" * 32)


def test_decode_v2_buy_base_is_token0() -> None:
    log_item = _v2_log(amount0_in=0, amount1_in=10**18, amount0_out=500 * 10**18, amount1_out=0)
    assert _decode_swap_log(log_item, base_is_token0=True) == (
        "buy",
        500 * 10**18,
        ROUTER.lower(),
    )


def test_decode_v2_sell_base_is_token0() -> None:
    log_item = _v2_log(amount0_in=500 * 10**18, amount1_in=0, amount0_out=0, amount1_out=10**18)
    assert _decode_swap_log(log_item, base_is_token0=True) == (
        "sell",
        500 * 10**18,
        ROUTER.lower(),
    )


def test_decode_v2_buy_base_is_token1() -> None:
    # Base is token1: a buy is amount1 out, amount0 in.
    log_item = _v2_log(amount0_in=10**18, amount1_in=0, amount0_out=0, amount1_out=500 * 10**18)
    assert _decode_swap_log(log_item, base_is_token0=False) == (
        "buy",
        500 * 10**18,
        ROUTER.lower(),
    )


def test_decode_v2_returns_none_for_non_swap() -> None:
    log_item = _v2_log(amount0_in=0, amount1_in=10**18, amount0_out=500 * 10**18, amount1_out=0)
    log_item["topics"] = ["0x" + "00" * 32, log_item["topics"][1], log_item["topics"][2]]
    assert _decode_swap_log(log_item, base_is_token0=True) is None


def test_decode_v3_buy_base_is_token0() -> None:
    # V3: amount0 negative (base out), amount1 positive (quote in).
    log_item = _v3_log(amount0=-(500 * 10**18), amount1=10**18)
    assert _decode_swap_log(log_item, base_is_token0=True) == (
        "buy",
        500 * 10**18,
        ROUTER.lower(),
    )


def test_decode_v3_sell_base_is_token0() -> None:
    log_item = _v3_log(amount0=500 * 10**18, amount1=-(10**18))
    assert _decode_swap_log(log_item, base_is_token0=True) == (
        "sell",
        500 * 10**18,
        ROUTER.lower(),
    )


def test_block_range_covers_five_minute_window(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHttp(responses=_rpc_responses(logs=[]))
    assert _block_range(
        fake,
        chain_slug="base",
        window_start=CREATED,
        window_end=CREATED + timedelta(minutes=5),
        max_blocks=2000,
    ) == (999850, 1_000_000)


def test_fetch_evm_pool_swaps_v2_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    buy = _v2_log(amount0_in=0, amount1_in=10**18, amount0_out=500 * 10**18, amount1_out=0)
    _patch_rpc(monkeypatch, responses=_rpc_responses(logs=[buy]))
    swaps = fetch_evm_pool_swaps(
        chain_slug="base",
        pool_address=POOL,
        base_address=BASE_TOKEN,
        quote_address=QUOTE_TOKEN,
        pair_created_at=CREATED,
        window_minutes=5,
        limit=8,
        price_usd=0.001,
    )
    assert len(swaps) == 1
    swap = swaps[0]
    assert swap["side"] == "buy"
    # The wallet is the transaction sender (the actual buyer), not the router in `to`.
    assert swap["wallet"] == BUYER.lower()
    assert swap["ts"] == SWAP_TS
    assert swap["size_usd"] == pytest.approx(0.5)


def test_fetch_evm_pool_swaps_v3_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    buy = _v3_log(amount0=-(500 * 10**18), amount1=10**18)
    _patch_rpc(monkeypatch, responses=_rpc_responses(logs=[buy]))
    swaps = fetch_evm_pool_swaps(
        chain_slug="base",
        pool_address=POOL,
        base_address=BASE_TOKEN,
        quote_address=QUOTE_TOKEN,
        pair_created_at=CREATED,
        window_minutes=5,
        limit=8,
        price_usd=0.001,
    )
    assert len(swaps) == 1
    assert swaps[0]["side"] == "buy"
    assert swaps[0]["size_usd"] == pytest.approx(0.5)


def test_fetch_evm_pool_swaps_ignores_out_of_window_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    # The log's block timestamp is after the window closed.
    buy = _v2_log(amount0_in=0, amount1_in=10**18, amount0_out=500 * 10**18, amount1_out=0)
    responses = _rpc_responses(logs=[buy])

    def block_by_number(params: list) -> dict:
        number = params[0]
        if number == hex(LATEST_BLOCK):
            ts = LATEST_TS
        else:
            # The swap block is 10 minutes after creation: outside the window.
            ts = CREATED + timedelta(minutes=10)
        return {"result": {"number": number, "timestamp": hex(int(ts.timestamp()))}}

    responses["eth_getBlockByNumber"] = block_by_number
    _patch_rpc(monkeypatch, responses=responses)
    swaps = fetch_evm_pool_swaps(
        chain_slug="base",
        pool_address=POOL,
        base_address=BASE_TOKEN,
        quote_address=QUOTE_TOKEN,
        pair_created_at=CREATED,
        window_minutes=5,
        limit=8,
        price_usd=0.001,
    )
    assert swaps == []


def test_fetch_evm_pool_swaps_returns_empty_for_non_evm_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_rpc(monkeypatch, responses=_rpc_responses(logs=[]))
    assert (
        fetch_evm_pool_swaps(
            chain_slug="solana",
            pool_address=POOL,
            base_address=BASE_TOKEN,
            quote_address=QUOTE_TOKEN,
            pair_created_at=CREATED,
        )
        == []
    )


def test_fetch_evm_pool_swaps_returns_empty_for_invalid_pool_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_rpc(monkeypatch, responses=_rpc_responses(logs=[]))
    assert (
        fetch_evm_pool_swaps(
            chain_slug="base",
            pool_address="0x" + "ab" * 32,  # V4 poolId, not an address.
            base_address=BASE_TOKEN,
            quote_address=QUOTE_TOKEN,
            pair_created_at=CREATED,
        )
        == []
    )


def test_fetch_evm_pool_swaps_returns_empty_when_window_in_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_rpc(monkeypatch, responses=_rpc_responses(logs=[]))
    assert (
        fetch_evm_pool_swaps(
            chain_slug="base",
            pool_address=POOL,
            base_address=BASE_TOKEN,
            quote_address=QUOTE_TOKEN,
            pair_created_at=LATEST_TS + timedelta(hours=1),
        )
        == []
    )


def test_fetch_evm_pool_swaps_fails_over_to_next_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buy = _v2_log(amount0_in=0, amount1_in=10**18, amount0_out=500 * 10**18, amount1_out=0)
    good = FakeHttp(responses=_rpc_responses(logs=[buy]))

    class FailingHttp(FakeHttp):
        def post_json(self, path: str, *, json: dict | None = None, headers: Any = None) -> Any:
            raise RuntimeError("fake endpoint down")

    first_url = "https://rpc1.example"
    second_url = "https://rpc2.example"

    def http_factory(*, base_url: str = "", **kwargs: Any) -> FakeHttp:
        return FailingHttp() if base_url == first_url else good

    class FakePool:
        def __init__(self) -> None:
            self.marked: list[tuple[str, str]] = []

        def snapshot(self) -> list:
            return [
                SimpleNamespace(url=first_url, down=False, health=1.0),
                SimpleNamespace(url=second_url, down=False, health=1.0),
            ]

        def record_request(self, url: str) -> None:
            pass

        def mark_success(self, url: str) -> None:
            self.marked.append(("ok", url))

        def mark_failure(self, url: str) -> None:
            self.marked.append(("fail", url))

    pool = FakePool()
    monkeypatch.setattr(evm_swaps, "HttpClient", http_factory)
    monkeypatch.setattr(evm_swaps, "get_rpc_url", lambda chain: first_url)
    monkeypatch.setattr(evm_swaps, "rpc_pool_for", lambda chain: pool)
    swaps = fetch_evm_pool_swaps(
        chain_slug="base",
        pool_address=POOL,
        base_address=BASE_TOKEN,
        quote_address=QUOTE_TOKEN,
        pair_created_at=CREATED,
        window_minutes=5,
        limit=8,
        price_usd=0.001,
    )
    assert len(swaps) == 1
    assert swaps[0]["side"] == "buy"
    assert ("fail", first_url) in pool.marked
    assert ("ok", second_url) in pool.marked
