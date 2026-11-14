"""Credential markers must not leak through logs or the trading status payload.

``test_secret_settings`` only covers repr/model_dump/fingerprint. This exercises
the live surfaces an operator or HTTP client actually sees: process logs and the
JSON returned by ``GET /trading/status`` with every provider credential set.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.config import get_settings
from common.logging import get_logger
from storage.database import get_session

_MARKERS = {
    "ENGINE_API_TOKEN": "MARKER-engine-token",
    "GMGN_API_KEY": "MARKER-gmgn-key",
    "GMGN_SIGNER_SECRET": "MARKER-gmgn-secret",
    "HELIUS_API_KEY": "MARKER-helius-key",
    "ALCHEMY_API_KEY": "MARKER-alchemy-key",
    "ETHERSCAN_API_KEY": "MARKER-etherscan-key",
    "REDDIT_CLIENT_SECRET": "MARKER-reddit-secret",
    "CMC_API_KEY": "MARKER-cmc-key",
    "GITHUB_TOKEN": "MARKER-github-token",
    "MINIO_SECRET_KEY": "MARKER-minio-secret",
    "BIRDEYE_API_KEY": "MARKER-birdeye-key",
}


@pytest.fixture
def credentialed_settings(monkeypatch):
    for name, value in _MARKERS.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.engine_api_token is not None
    yield settings
    get_settings.cache_clear()


@pytest.fixture
def client(session, credentialed_settings, monkeypatch):
    monkeypatch.delenv("ENGINE_API_AUTH_BYPASS", raising=False)
    get_settings.cache_clear()
    monkeypatch.setenv("ENGINE_API_TOKEN", _MARKERS["ENGINE_API_TOKEN"])

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app, headers={"Authorization": f"Bearer {_MARKERS['ENGINE_API_TOKEN']}"})
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_status_payload_and_logs_contain_no_credential_markers(
    client, credentialed_settings, capfd
) -> None:
    logger = get_logger("tests.credential_leak")
    logger.info("credential_leak_probe", component="test")

    response = client.get("/trading/status")

    logger.info("credential_leak_probe_finished", component="test")
    captured = capfd.readouterr()
    logs = captured.out + captured.err

    assert response.status_code == 200, response.text
    # The credentials really are configured, so the absence assertions below
    # are not passing against an empty settings object.
    assert (
        credentialed_settings.helius_api_key is not None
        and credentialed_settings.helius_api_key.get_secret_value() == _MARKERS["HELIUS_API_KEY"]
    )
    # The probe proves log capture is live, so the marker assertions are not
    # passing against an empty buffer.
    assert "credential_leak_probe" in logs
    assert "credential_leak_probe_finished" in logs

    for marker in _MARKERS.values():
        assert marker not in response.text
        assert marker not in logs
