from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.config import get_settings
from storage import models
from storage.database import get_session
from storage.repository import get_or_create_chain, upsert_asset
from tests.conftest import seed_market_asset
from trading.candidates import CANDIDATE_KEYS, build_candidates, get_candidate
from trading.price_sources import MarketSnapshotPriceSource

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_TOKEN = "test-api-token-not-a-secret"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


def test_unknown_mint_is_none(session) -> None:
    source = MarketSnapshotPriceSource(session)
    assert get_candidate(session, "does-not-exist", now=NOW, price_source=source) is None


def test_candidate_has_exact_keys(session) -> None:
    asset = seed_market_asset(session)
    session.commit()

    candidate = get_candidate(
        session,
        asset.address,
        now=NOW,
        price_source=MarketSnapshotPriceSource(session),
    )
    assert candidate is not None
    assert tuple(candidate) == CANDIDATE_KEYS
    assert candidate["mint"] == asset.address
    assert candidate["chain"] == "solana"
    assert candidate["missing"]  # no score yet
    assert candidate["evidence_ts"] is not None


def test_build_candidates_respects_n(session) -> None:
    seed_market_asset(session)
    seed_market_asset(session, address="Token222222222222222222222222222222222222", symbol="TWO")
    session.commit()

    feed = build_candidates(session, n=1, now=NOW, price_source=MarketSnapshotPriceSource(session))
    assert set(feed) == {"candidates", "excluded_no_evidence", "generated_at"}
    assert len(feed["candidates"]) == 1
    assert tuple(feed["candidates"][0]) == CANDIDATE_KEYS


def test_missing_top10_is_none_and_listed(session) -> None:
    asset = seed_market_asset(session)
    session.commit()

    candidate = get_candidate(
        session,
        asset.address,
        now=NOW,
        price_source=MarketSnapshotPriceSource(session),
    )

    assert candidate is not None
    assert candidate["top10_holder_pct"] is None
    assert "top10_holder_pct" in candidate["missing"]
    assert tuple(candidate) == CANDIDATE_KEYS


def test_asset_without_timestamped_row_is_excluded(session) -> None:
    chain = get_or_create_chain(
        session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
    )
    upsert_asset(
        session,
        chain_id=chain.id,
        address="NoEvidence111111111111111111111111111111",
        symbol="NOEV",
        name="No Evidence",
        first_seen_at=NOW - timedelta(hours=1),
    )
    session.commit()

    feed = build_candidates(
        session,
        n=10,
        now=NOW,
        price_source=MarketSnapshotPriceSource(session),
    )

    assert feed["candidates"] == []
    assert feed["excluded_no_evidence"] == 1


def test_insufficient_forecast_has_no_value(session) -> None:
    asset = seed_market_asset(session)
    session.add(
        models.Forecast(
            asset_id=asset.id,
            decision_ts=NOW - timedelta(hours=1),
            observed_at=NOW - timedelta(hours=1),
            p_ignition_24h=0.9,
            p_collapse_24h=0.1,
            details={"status": "insufficient_data"},
            model_version="test-v1",
        )
    )
    session.commit()

    candidate = get_candidate(
        session,
        asset.address,
        now=NOW,
        price_source=MarketSnapshotPriceSource(session),
    )

    assert candidate is not None
    assert candidate["forecast"]["value"] is None
    assert candidate["forecast"]["insufficient_data"] is True


def test_no_live_price_is_untradeable(session) -> None:
    asset = seed_market_asset(session)
    session.commit()

    class _NoPrice:
        def price(self, mint: str, chain: str):
            return None

    candidate = get_candidate(session, asset.address, now=NOW, price_source=_NoPrice())

    assert candidate is not None
    assert candidate["tradeable"] is False
    assert candidate["tradeable_reason"] == "no_live_price"


@pytest.fixture()
def client(session, tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_API_TOKEN", _TOKEN)
    monkeypatch.delenv("ENGINE_API_AUTH_BYPASS", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "trading_rules_path", str(tmp_path / "absent.json"))

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app, headers=_HEADERS)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_candidate_trade_without_rules_is_409(client, session) -> None:
    asset = seed_market_asset(session)
    session.commit()

    response = client.post(
        f"/paper/candidates/{asset.address}/trade",
        json={"why_entered": "test"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "rules_incomplete"


def test_candidate_live_trade_with_closed_gate_is_403(
    client, session, monkeypatch, tmp_path
) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps({"buy_trigger": "ignition_detected", "stop_pct": 20, "target_pct": 50})
    )
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "trading_rules_path", str(rules))
    asset = seed_market_asset(session)
    session.commit()

    response = client.post(
        f"/paper/candidates/{asset.address}/trade",
        json={"why_entered": "test", "mode": "live"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "live_disabled"


def test_trading_execution_is_not_imported_by_engine_or_ingestion() -> None:
    files = [Path("engine/run.py")] + sorted(Path("ingestion").rglob("*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "trading.ledger" not in text
        assert "trading.gmgn_exec" not in text
