from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.config import get_settings
from ingestion.market_analysis import (
    build_market_evidence,
    select_dexscreener_pair,
)
from llm.engine import LLMPredictionEngine
from storage.database import get_session
from tests.conftest import seed_market_asset
from ui.gmgn import gmgn_token_url


@pytest.fixture
def market_client(session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENGINE_API_TOKEN", "analysis-test-token")
    monkeypatch.delenv("ENGINE_API_AUTH_BYPASS", raising=False)
    get_settings.cache_clear()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app, headers={"Authorization": "Bearer analysis-test-token"})
    finally:
        app.dependency_overrides.pop(get_session, None)
        get_settings.cache_clear()


def test_asset_search_matches_symbol_and_contract_address(market_client, session) -> None:
    asset = seed_market_asset(session, address="MarketAnalysisToken123", symbol="MKT")
    session.commit()
    by_symbol = market_client.get("/assets/search", params={"q": "mkt"})
    by_address = market_client.get("/assets/search", params={"q": "AnalysisToken"})
    assert by_symbol.status_code == 200
    assert by_symbol.json()[0]["asset_id"] == asset.id
    assert by_address.json()[0]["address"] == asset.address


def test_analysis_persists_market_report_and_history_when_llm_disabled(
    market_client, session, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = seed_market_asset(session)
    seed_market_asset(
        session,
        low_liquidity=True,
        address="OtherToken111111111111111111111111111111111111",
        symbol="OTHER",
        pair_address="OtherPair111111111111111111111111111111111111",
    )
    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()
    pair = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "Pair111111111111111111111111111111111111",
        "baseToken": {"address": asset.address, "symbol": asset.symbol, "name": asset.name},
        "priceUsd": "2.5",
        "liquidity": {"usd": 12000},
    }

    class FakeDexScreenerClient:
        def token_pairs(self, chain: str, address: str) -> list[dict]:
            assert chain == "solana"
            assert address == asset.address
            return [pair]

        def close(self) -> None:
            pass

    monkeypatch.setattr("ingestion.source_clients.DexScreenerClient", FakeDexScreenerClient)
    response = market_client.post("/market-analysis/analyze", json={"asset_id": asset.id})
    assert response.status_code == 200
    result = response.json()
    assert result["narrative_status"] == "unavailable"
    assert result["evidence"]["live"]["price_usd"] == 2.5
    assert result["evidence"]["history"][0]["liquidity_usd"] == 120000
    history = market_client.get(f"/market-analysis/{asset.id}/history")
    assert history.status_code == 200
    assert history.json()[0]["run_id"] == result["run_id"]
    saved = market_client.get(f"/market-analysis/runs/{result['run_id']}")
    assert saved.json()["evidence"] == result["evidence"]


def test_analysis_resolves_and_saves_unknown_contract_address(
    market_client, session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_market_asset(session)
    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()
    address = "0xabcdef0123456789abcdef0123456789abcdef01"
    pair = {
        "chainId": "base",
        "dexId": "uniswap",
        "pairAddress": "0xpair123",
        "baseToken": {"address": address, "symbol": "NEW", "name": "New Token"},
        "quoteToken": {"address": "0xquote", "symbol": "USD", "name": "Dollar"},
        "priceUsd": "0.25",
        "volume": {"h24": "25000"},
        "liquidity": {"usd": "10000"},
    }

    class FakeDexScreenerClient:
        def search_pairs(self, query: str) -> list[dict]:
            assert query == address
            return [pair]

        def token_pairs(self, chain: str, token_address: str) -> list[dict]:
            assert chain == "base"
            assert token_address.casefold() == address.casefold()
            return [pair]

        def close(self) -> None:
            pass

    monkeypatch.setattr("ingestion.source_clients.DexScreenerClient", FakeDexScreenerClient)
    response = market_client.post(
        "/market-analysis/analyze",
        json={"address": address, "chain": "base", "pair_address": "0xpair123"},
    )
    assert response.status_code == 200
    assert response.json()["symbol"] == "NEW"
    assert response.json()["evidence"]["history"]
    found = market_client.get("/assets/search", params={"q": address})
    assert found.json()[0]["address"] == address


def test_contract_search_returns_dexscreener_candidates_for_unstored_ca(
    market_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "0xabcdef0123456789abcdef0123456789abcdef02"
    pair = {
        "chainId": "base",
        "dexId": "uniswap",
        "pairAddress": "0xpair456",
        "baseToken": {"address": address, "symbol": "NEW", "name": "New Token"},
        "liquidity": {"usd": "12000"},
    }

    class FakeDexScreenerClient:
        def search_pairs(self, query: str) -> list[dict]:
            assert query == address
            return [pair]

        def close(self) -> None:
            pass

    monkeypatch.setattr("ingestion.source_clients.DexScreenerClient", FakeDexScreenerClient)
    response = market_client.get("/assets/search", params={"q": address})
    assert response.status_code == 200
    assert response.json()[0]["external"] is True
    assert response.json()[0]["pair_address"] == "0xpair456"


def test_independent_narrative_prompt_uses_only_provided_market_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object.__new__(LLMPredictionEngine)
    prompts: list[str] = []
    monkeypatch.setattr(
        engine,
        "_call_provider",
        lambda prompt, max_tokens=512: (
            prompts.append(prompt)
            or '{"narrative":"Market activity is present in the supplied snapshot.",'
            '"risk_assessment":"Evidence is limited to the supplied market snapshot."}'
        ),
    )
    narrative, risk = engine.market_analysis_narrative({"live": {"price_usd": 1.5}, "history": []})
    assert narrative.startswith("Market activity")
    assert risk.startswith("Evidence is limited")
    assert '"price_usd": 1.5' in prompts[0]
    assert "forecast" not in prompts[0].lower()
    assert "score" not in prompts[0].lower()


def test_gmgn_url_maps_supported_chains_and_rejects_unknown() -> None:
    assert gmgn_token_url("solana", "TokenCA") == "https://gmgn.ai/sol/token/TokenCA"
    assert gmgn_token_url("base", "0xAbC") == "https://gmgn.ai/base/token/0xAbC"
    assert gmgn_token_url("ethereum", "0xAbC") == "https://gmgn.ai/eth/token/0xAbC"
    assert gmgn_token_url("unknown", "TokenCA") is None


def test_select_dexscreener_pair_matches_contract_and_optional_chain() -> None:
    pairs = [
        {"chainId": "base", "baseToken": {"address": "0xOther"}},
        {"chainId": "ethereum", "baseToken": {"address": "0xAbC"}},
    ]
    assert select_dexscreener_pair(pairs, "0xabc", "ethereum") == pairs[1]
    assert select_dexscreener_pair(pairs, "0xabc", "base") is None


def test_market_evidence_keeps_live_and_stored_timestamps_and_missing_values() -> None:
    observed = datetime(2026, 10, 26, 12, tzinfo=UTC)
    evidence = build_market_evidence(
        {"chainId": "base", "priceUsd": "1.25", "liquidity": {"usd": "5000"}},
        [
            {
                "ts": observed,
                "observed_at": observed,
                "price_usd": 1.0,
                "volume_usd": None,
                "trades": 3,
            }
        ],
        fetched_at=datetime(2026, 10, 27, 12, tzinfo=UTC),
    )
    assert evidence["live"]["price_usd"] == 1.25
    assert evidence["live"]["observed_at"]
    assert evidence["live"]["liquidity_usd"] == 5000.0
    assert evidence["history"][0]["observed_at"] == observed.isoformat()
    assert evidence["history"][0]["freshness"] == "stale"
    assert evidence["history"][0]["volume_usd"] is None
