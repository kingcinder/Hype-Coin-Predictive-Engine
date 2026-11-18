"""Credential markers must not leak through logs or the trading status payload.

``test_secret_settings`` only covers repr/model_dump/fingerprint. This exercises
the live surfaces an operator or HTTP client actually sees: process logs and the
JSON returned by ``GET /trading/status`` with every provider credential set.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.config import get_settings, secret_value
from common.logging import configure_logging, get_logger
from ingestion import service as ingestion_service
from ingestion.service import IngestionService
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


def test_http_request_lines_cannot_log_provider_urls() -> None:
    """httpx INFO lines embed full URLs (and their keys); keep them suppressed."""
    configure_logging()
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level >= logging.WARNING


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
    for env_name, marker in _MARKERS.items():
        configured = getattr(credentialed_settings, env_name.lower())
        assert secret_value(configured) == marker, env_name
    # The probe proves log capture is live, so the marker assertions are not
    # passing against an empty buffer.
    assert "credential_leak_probe" in logs
    assert "credential_leak_probe_finished" in logs

    for marker in _MARKERS.values():
        assert marker not in response.text
        assert marker not in logs


def test_provider_http_failure_does_not_leak_url_key_to_logs_or_health_payload(
    client, session, credentialed_settings, monkeypatch, capfd
) -> None:
    """Provider HTTP errors include request URLs; both telemetry surfaces redact keys."""
    marker = _MARKERS["BIRDEYE_API_KEY"]
    request = httpx.Request(
        "GET",
        f"https://public-api.birdeye.so/defi/tokens/new_listing?api-key={marker}",
    )
    response = httpx.Response(500, request=request)
    with pytest.raises(httpx.HTTPStatusError) as provider_error:
        response.raise_for_status()
    assert marker in str(provider_error.value)

    class FailingBirdeyeClient:
        BASE_URL = "https://public-api.birdeye.so"

        def __init__(self, api_key: str | None = None) -> None:
            assert api_key == marker

        def new_tokens(self, limit: int = 20) -> list[dict]:
            raise provider_error.value

        def close(self) -> None:
            pass

    monkeypatch.setattr(ingestion_service, "BirdeyeClient", FailingBirdeyeClient)
    service = IngestionService()
    assert service._ingest_birdeye_solana(session) == 0
    session.commit()

    response = client.get("/health")
    captured = capfd.readouterr()
    logs = captured.out + captured.err
    assert response.status_code == 200, response.text
    assert "birdeye_ingestion_failed" in logs
    for marker_value in _MARKERS.values():
        assert marker_value not in response.text
        assert marker_value not in logs
    assert "api-key=***" in response.text
    assert "api-key=***" in logs


def test_dexscreener_http_failure_does_not_leak_url_key_to_logs_or_health_payload(
    client, session, credentialed_settings, monkeypatch, capfd
) -> None:
    """A second ingestion provider path must keep its error URL out of /health."""
    marker = _MARKERS["HELIUS_API_KEY"]
    request = httpx.Request(
        "GET",
        f"https://api.dexscreener.com/latest/dex/token-profiles?api-key={marker}",
    )
    response = httpx.Response(500, request=request)
    with pytest.raises(httpx.HTTPStatusError) as provider_error:
        response.raise_for_status()
    assert marker in str(provider_error.value)

    class FailingDexScreenerClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def latest_token_profiles(self):
            raise provider_error.value

        def close(self) -> None:
            pass

    monkeypatch.setattr(ingestion_service, "DexScreenerClient", FailingDexScreenerClient)
    service = IngestionService()
    service.ensure_reference_data(session)
    assert service._ingest_dexscreener_profiles(session) == 0
    session.commit()

    health_response = client.get("/health")
    captured = capfd.readouterr()
    logs = captured.out + captured.err
    assert health_response.status_code == 200, health_response.text
    assert "dexscreener_profiles_failed" in logs
    for marker_value in _MARKERS.values():
        assert marker_value not in health_response.text
        assert marker_value not in logs
    assert "api-key=***" in health_response.text
    assert "api-key=***" in logs
