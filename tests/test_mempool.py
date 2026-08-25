from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import respx
from httpx import Response
from sqlalchemy import func, select

from ingestion.rpc_pool import RpcEndpointPool
from mempool.evm import PAIR_CREATED_TOPIC, EVMFactoryWatcher
from mempool.solana import SolanaMempoolWatcher
from storage import models
from tests.conftest import seed_market_asset

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
SOLANA_RPC = "https://api.mainnet-beta.solana.com/"


def _pin_rpc_pool(monkeypatch, *, chain: str = "solana", url: str = SOLANA_RPC) -> None:
    """Pin one chain's §2 endpoint pool to the mocked URL so respx intercepts it."""

    def fake_pool(chain_slug: str) -> RpcEndpointPool:
        if chain_slug == chain:
            return RpcEndpointPool([url], failure_threshold=2)
        return RpcEndpointPool([], failure_threshold=2)

    monkeypatch.setattr("ingestion.source_clients.get_rpc_pool", fake_pool)


def test_solana_mempool_detects_burst_and_is_idempotent(session, monkeypatch) -> None:
    _pin_rpc_pool(monkeypatch)
    from ingestion.service import IngestionService

    IngestionService().ensure_reference_data(session)
    asset = seed_market_asset(
        session,
        address="TokenMempool1111111111111111111111111111",
        symbol="MPOOL",
        pair_address="PairMempool1111111111111111111111111111",
    )
    pool = session.scalar(select(models.Pool).where(models.Pool.base_asset_id == asset.id))
    assert pool is not None
    pool.created_at_source = NOW - timedelta(seconds=60)
    session.commit()

    signatures = [
        {
            "signature": f"sig{index}",
            "blockTime": int((NOW - timedelta(seconds=90 - index)).timestamp()),
        }
        for index in range(20)
    ]

    with respx.mock:
        respx.post(SOLANA_RPC).mock(
            return_value=Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": signatures},
            )
        )
        source = session.scalar(select(models.Source).where(models.Source.name == "solana_rpc"))
        assert source is not None
        watcher = SolanaMempoolWatcher()
        result = watcher.watch_asset(session, asset=asset, source=source, decision_ts=NOW)
        session.commit()
        assert result["burst"] is True
        assert result["new_signatures"] == 20
        assert session.scalar(select(func.count()).select_from(models.IgnitionEvent)) == 1

        # second watch sees the same signatures -> watermark dedupes, no new event
        result = watcher.watch_asset(session, asset=asset, source=source, decision_ts=NOW)
        session.commit()
        assert result["new_signatures"] == 0
        assert session.scalar(select(func.count()).select_from(models.IgnitionEvent)) == 1


def test_solana_mempool_quiet_mint_has_no_burst(session, monkeypatch) -> None:
    _pin_rpc_pool(monkeypatch)
    from ingestion.service import IngestionService

    IngestionService().ensure_reference_data(session)
    asset = seed_market_asset(
        session,
        address="TokenQuiet2 111111111111111111111111111111",
        symbol="QUIET2",
        pair_address="PairQuiet211111111111111111111111111111",
    )
    pool = session.scalar(select(models.Pool).where(models.Pool.base_asset_id == asset.id))
    assert pool is not None
    pool.created_at_source = NOW - timedelta(seconds=60)
    session.commit()
    signatures = [
        {"signature": f"q{index}", "blockTime": int((NOW - timedelta(seconds=10)).timestamp())}
        for index in range(5)
    ]
    with respx.mock:
        respx.post(SOLANA_RPC).mock(
            return_value=Response(200, json={"jsonrpc": "2.0", "id": 1, "result": signatures})
        )
        source = session.scalar(select(models.Source).where(models.Source.name == "solana_rpc"))
        watcher = SolanaMempoolWatcher()
        result = watcher.watch_asset(session, asset=asset, source=source, decision_ts=NOW)
        session.commit()
        assert result["burst"] is False
        assert session.scalar(select(func.count()).select_from(models.IgnitionEvent)) == 0


def _topic_address(address: str) -> str:
    return "0x" + "0" * 24 + address


def _uint_word(value: int) -> str:
    return "0x" + f"{value:064x}"


def test_evm_factory_watcher_seeds_pool_from_pair_created(session, monkeypatch) -> None:
    _pin_rpc_pool(monkeypatch, chain="base", url="https://mainnet.base.org")
    from ingestion.service import IngestionService

    service = IngestionService()
    service.ensure_reference_data(session)
    session.flush()
    source = session.scalar(select(models.Source).where(models.Source.name == "evm_rpc"))
    assert source is not None

    token0 = "aaaabbbbccccddddeeeeffff0000111122223333"
    token1 = "1111222233334444555566667777888899990000"
    pair = "ffff000011112222333344445555666677778888"
    factory = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
    log_payload = {
        "address": factory.lower(),
        "topics": [
            PAIR_CREATED_TOPIC,
            _topic_address(token0),
            _topic_address(token1),
            _topic_address(pair),
        ],
        "data": _uint_word(1),
        "blockNumber": "0x10",
        "logIndex": "0x0",
        "transactionHash": "0xabc",
    }

    def symbol_response(symbol: str) -> str:
        encoded = symbol.encode().hex()
        padded = encoded + "0" * (64 - len(encoded))
        return "0x" + _uint_word(32)[2:] + _uint_word(len(symbol))[2:] + padded

    def rpc_handler(request):
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "eth_blockNumber":
            return Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x100"})
        if method == "eth_getLogs":
            return Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [log_payload]})
        if method == "eth_call":
            params = payload["params"][0]
            data = params["data"]
            if data == "0x95d89b41":  # symbol()
                return Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": symbol_response(
                            "MEME" if params["to"].endswith(token0) else "USDC"
                        ),
                    },
                )
            if data == "0x313ce567":  # decimals()
                return Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _uint_word(6)})
            if data == "0x0902f1ac":  # getReserves()
                return Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": _uint_word(1_000) + _uint_word(5_000_000)[2:],
                    },
                )
            return Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x"})
        return Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})

    with respx.mock:
        respx.post("https://mainnet.base.org/").mock(side_effect=rpc_handler)
        watcher = EVMFactoryWatcher("base", factory)
        try:
            count = watcher.watch(session, source=source, decision_ts=NOW)
        finally:
            watcher.close()
        session.commit()
        assert count == 1

    asset = session.scalar(select(models.Asset).where(models.Asset.symbol == "MEME"))
    assert asset is not None
    pool = session.scalar(select(models.Pool).where(models.Pool.address == "0x" + pair))
    assert pool is not None
    snapshots = session.scalars(
        select(models.LiquiditySnapshot).where(models.LiquiditySnapshot.pool_id == pool.id)
    ).all()
    assert len(snapshots) == 1
    assert round(float(snapshots[0].reserve_usd or 0.0), 2) == 5.0
