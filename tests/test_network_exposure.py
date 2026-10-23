from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.config import Settings, get_settings
from common.network import validate_network_exposure
from ui import live


def test_api_and_ui_bind_defaults_are_loopback() -> None:
    settings = Settings(_env_file=None)
    assert settings.api_host == "127.0.0.1"
    assert settings.ui_host == "127.0.0.1"


def test_lan_bind_is_allowed_only_with_explicit_authenticated_config() -> None:
    settings = Settings(
        _env_file=None,
        api_host="0.0.0.0",
        ui_host="0.0.0.0",
        engine_allow_lan=True,
        engine_api_token="lan-test-token",
    )
    validate_network_exposure(settings)


def test_startup_rejects_lan_bind_with_bypass_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    monkeypatch.setenv("ENGINE_ALLOW_LAN", "1")
    monkeypatch.setenv("ENGINE_API_AUTH_BYPASS", "1")
    monkeypatch.setenv("ENGINE_API_TOKEN", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="ENGINE_API_TOKEN"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()


def test_gui_sse_subscriber_sends_token_and_updates_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "ui-test-token"
    monkeypatch.setenv("ENGINE_API_TOKEN", token)
    get_settings.cache_clear()
    monkeypatch.setattr(live, "API_BASE_URL", "http://engine.test")
    previous = live.engine_snapshot()
    live._engine_state.clear()
    requests: list[httpx.Request] = []

    class StopWorker(Exception):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) > 1:
            raise RuntimeError("stop after first SSE payload")
        if request.headers.get("Authorization") != f"Bearer {token}":
            return httpx.Response(401)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"type":"update","status":"running"}\n\n',
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def mock_client(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(live.httpx, "Client", mock_client)

    def stop_after_retry(_seconds: float) -> None:
        raise StopWorker

    monkeypatch.setattr(live.time, "sleep", stop_after_retry)
    try:
        with pytest.raises(StopWorker):
            live._sse_worker()
        assert requests[0].url.path == "/engine/stream"
        assert requests[0].headers["Authorization"] == f"Bearer {token}"
        assert live.engine_snapshot()["status"] == "running"
    finally:
        live._engine_state.clear()
        live._engine_state.update(previous)
        get_settings.cache_clear()
