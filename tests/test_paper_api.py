from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from api.main import app
from common.config import get_settings
from storage.database import get_session
from storage.write_queue import start_write_queue, stop_write_queue
from tests.conftest import seed_market_asset
from trading.candidates import CANDIDATE_KEYS

_TOKEN = "test-api-token-not-a-secret"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
def client(session, tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_API_TOKEN", _TOKEN)
    monkeypatch.delenv("ENGINE_API_AUTH_BYPASS", raising=False)
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps({"buy_trigger": "ignition_detected", "stop_pct": 20, "target_pct": 50})
    )
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "trading_rules_path", str(rules))

    monkeypatch.setattr(
        "api.trading_routes.utc_now",
        lambda: datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )
    factory = sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)
    start_write_queue(session_factory=factory)

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app, headers=_HEADERS)
    finally:
        app.dependency_overrides.clear()
        stop_write_queue()
        get_settings.cache_clear()


def test_candidates_endpoint(client, session) -> None:
    asset = seed_market_asset(session)
    session.commit()

    response = client.get("/paper/candidates")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"candidates", "excluded_no_evidence", "generated_at"}
    assert body["candidates"][0]["mint"] == asset.address
    assert set(body["candidates"][0]) == set(CANDIDATE_KEYS)


def test_open_then_ledger_and_scorecard(client, session) -> None:
    asset = seed_market_asset(session)
    session.commit()

    opened = client.post(
        f"/paper/candidates/{asset.address}/trade",
        json={"why_entered": "api test"},
    )
    assert opened.status_code == 200, opened.text
    position_id = opened.json()["position_id"]

    ledger = client.get("/paper/ledger")
    assert ledger.status_code == 200
    assert ledger.json()["open"][0]["id"] == position_id

    exit_response = client.post(
        f"/paper/trades/{position_id}/exit",
        json={"why_exited": "test", "lesson": "test"},
    )
    assert exit_response.status_code == 200, exit_response.text

    card = client.get("/paper/scorecard")
    assert card.status_code == 200
    assert card.json()["trades"] == 1
    assert Decimal(str(card.json()["net_pnl_sol"])) <= Decimal("0")


def test_missing_rules_is_409(client, session, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ENGINE_API_TOKEN", _TOKEN)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "trading_rules_path", str(tmp_path / "absent.json"))
    asset = seed_market_asset(session)
    session.commit()

    response = client.post(f"/paper/candidates/{asset.address}/trade", json={"why_entered": "x"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "rules_incomplete"


def test_execute_default_settings_is_403(client) -> None:
    response = client.post(
        "/trading/execute",
        json={
            "mint": "TEST",
            "chain": "solana",
            "side": "buy",
            "size_sol": "1",
            "slippage_pct": "0.5",
            "priority_fee_sol": "0.001",
            "why": "test",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "live_disabled"
