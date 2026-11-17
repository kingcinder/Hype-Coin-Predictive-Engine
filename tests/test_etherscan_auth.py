"""Etherscan V2 rejects unauthenticated calls, so every call must carry the key."""

from __future__ import annotations

from typing import Any, cast

import respx
from httpx import Response

from common.config import get_settings
from crawlers.sources import whale_tracker as whale_mod
from crawlers.sources.gas_tracker import GasTrackerCrawler
from crawlers.sources.whale_tracker import ETH_WHALE_ADDRESSES, WhaleTrackerCrawler
from ingestion.contract_analyzer import (
    ContractAnalysis,
    _analyze_evm_contract,
    _check_deployer_history,
)
from ingestion.holder_tracker import get_evm_holders

KEY = "test-etherscan-key"


class FakeHttp:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, path, *, params=None, headers=None):
        self.calls.append((path, dict(params or {})))
        return self.payload

    def close(self) -> None:
        pass


def test_holder_lookup_sends_chain_id_and_key(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "etherscan_api_key", KEY)
    http = FakeHttp({"status": "1", "result": []})

    get_evm_holders("0xtoken", "ethereum", http=cast(Any, http))

    path, params = http.calls[0]
    assert path == "/v2/api"
    assert params["chainid"] == "1"
    assert params["action"] == "tokentx"
    assert params["apikey"] == KEY


def test_contract_source_lookup_sends_chain_id_and_key(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "etherscan_api_key", KEY)
    http = FakeHttp({"status": "1", "result": [{"SourceCode": ""}]})

    _analyze_evm_contract("0xtoken", "base", ContractAnalysis(), cast(Any, http))

    path, params = http.calls[0]
    assert path == "/v2/api"
    assert params["chainid"] == "8453"
    assert params["action"] == "getsourcecode"
    assert params["apikey"] == KEY
    # The deployer txlist follow-up hit V2 unauthenticated before this fix.
    txlist_params = http.calls[1][1]
    assert txlist_params["action"] == "txlist"
    assert {"endblock", "page", "startblock"}.isdisjoint(txlist_params)
    for _, call_params in http.calls:
        assert call_params["apikey"] == KEY


def test_evm_contract_exposes_etherscan_auth_failure() -> None:
    result = ContractAnalysis()

    _analyze_evm_contract(
        "0xtoken",
        "base",
        result,
        cast(
            Any, FakeHttp({"status": "0", "message": "NOTOK", "result": "Missing/Invalid API Key"})
        ),
    )

    assert result.error == "Missing/Invalid API Key"
    assert result.suspicious_flags == 0


def test_deployer_history_lookup_sends_key(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "etherscan_api_key", KEY)
    http = FakeHttp({"status": "1", "result": []})

    _check_deployer_history("0xdeployer", cast(Any, http), "1", ContractAnalysis())

    _, params = http.calls[0]
    assert params["chainid"] == "1"
    assert params["apikey"] == KEY
    assert {"endblock", "page", "startblock"}.isdisjoint(params)


@respx.mock
def test_gas_oracle_uses_v2_with_key() -> None:
    route = respx.get("https://api.etherscan.io/v2/api").mock(
        return_value=Response(
            200,
            json={
                "status": "1",
                "result": {
                    "SafeGasPrice": "1",
                    "ProposeGasPrice": "2",
                    "FastGasPrice": "3",
                    "suggestBaseFee": "1",
                },
            },
        )
    )
    crawler = GasTrackerCrawler(
        include_solana=False, include_pending_tx=False, etherscan_api_key=KEY
    )
    try:
        items = crawler.fetch()
    finally:
        crawler.close()

    assert items
    params = route.calls[0].request.url.params
    assert params["chainid"] == "1"
    assert params["apikey"] == KEY


@respx.mock
def test_whale_tracker_maps_large_transfers_from_every_watched_address(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "etherscan_api_key", KEY)
    monkeypatch.setattr(whale_mod.time, "sleep", lambda _seconds: None)
    tx = {
        "value": str(60 * 10**18),
        "timeStamp": "1700000000",
        "hash": "0xabc",
        "from": "0xfrom",
        "to": "0xto",
        "blockNumber": "123",
    }
    respx.get("https://api.etherscan.io/v2/api").mock(
        return_value=Response(200, json={"status": "1", "result": [tx]})
    )
    crawler = WhaleTrackerCrawler()
    try:
        items = crawler._fetch_eth_transfers()
    finally:
        crawler.close()

    assert len(items) == len(whale_mod.ETH_WATCH_TARGETS)
    assert items[-1]["title"] == "Large DEX swap: 60.0 ETH via Uniswap"
    assert items[-1]["metrics"]["dex"] == "uniswap_v2"
    assert items[0]["metrics"]["from"] == "0xfrom"


@respx.mock
def test_whale_tracker_uses_authenticated_v2(monkeypatch) -> None:
    """Whale txlist calls moved off the retired unauthenticated V1 endpoint."""
    monkeypatch.setattr(get_settings(), "etherscan_api_key", KEY)
    route = respx.get("https://api.etherscan.io/v2/api").mock(
        return_value=Response(200, json={"status": "1", "result": []})
    )
    crawler = WhaleTrackerCrawler()
    try:
        assert crawler._fetch_eth_transfers() == []
    finally:
        crawler.close()

    assert len(route.calls) == len(ETH_WHALE_ADDRESSES) + 1
    for call in route.calls:
        params = call.request.url.params
        assert params["chainid"] == "1"
        assert params["apikey"] == KEY
