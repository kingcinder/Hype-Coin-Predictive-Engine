"""Etherscan V2 rejects unauthenticated calls, so every call must carry the key."""

from __future__ import annotations

import respx
from httpx import Response

from common.config import get_settings
from crawlers.sources.gas_tracker import GasTrackerCrawler
from ingestion.contract_analyzer import ContractAnalysis, _analyze_evm_contract
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

    get_evm_holders("0xtoken", "ethereum", http=http)

    path, params = http.calls[0]
    assert path == "/v2/api"
    assert params["chainid"] == "1"
    assert params["action"] == "tokentx"
    assert params["apikey"] == KEY


def test_contract_source_lookup_sends_chain_id_and_key(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "etherscan_api_key", KEY)
    http = FakeHttp({"status": "1", "result": [{"SourceCode": ""}]})

    _analyze_evm_contract("0xtoken", "base", ContractAnalysis(), http)

    path, params = http.calls[0]
    assert path == "/v2/api"
    assert params["chainid"] == "8453"
    assert params["action"] == "getsourcecode"
    assert params["apikey"] == KEY


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
