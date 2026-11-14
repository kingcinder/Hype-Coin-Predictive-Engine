"""HTTP error text must not leak credentials embedded in request URLs."""

from __future__ import annotations

import httpx

from data_lake import webhooks as webhook_mod
from data_lake.webhooks import dispatch_webhook
from llm.engine import LLMPredictionEngine
from ops import notifier as notifier_mod
from ops.notifier import _redact_ntfy_error
from storage import models

_WEBHOOK_PATH_SECRET = "x" * 24
_DISCORD_URL = f"https://discord.com/api/webhooks/123456789012345678/{_WEBHOOK_PATH_SECRET}"
_DISCORD_TOKEN = _WEBHOOK_PATH_SECRET


def test_llm_health_error_redacts_provider_url() -> None:
    """A custom LLM base URL can carry a path token; health must not echo it."""
    engine = LLMPredictionEngine()
    engine._base_url = "https://llm.example/v2/fake-llm-path-token?api_key=fake-query-key"  # noqa: SLF001

    def _boom(*args, **kwargs):
        raise RuntimeError(
            f"Client error for url '{engine._base_url}/v1/models'"  # noqa: SLF001
        )

    engine._retry_request = _boom  # type: ignore[method-assign]  # noqa: SLF001

    health = engine.check_health()

    assert "fake-llm-path-token" not in health.error
    assert "fake-query-key" not in health.error
    assert "/v2/***" in health.error
    assert "api_key=***" in health.error


class _FailingClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def post(self, url, content=None, headers=None):
        raise httpx.ConnectError(
            f"Connection error for url '{url}'",
            request=httpx.Request("POST", url),
        )


def test_ntfy_error_redacts_topic_and_query_credentials(monkeypatch) -> None:
    class _Settings:
        ntfy_topic = "secret-topic"

    monkeypatch.setattr(notifier_mod, "get_settings", lambda: _Settings())

    text = _redact_ntfy_error(
        RuntimeError("Client error for url 'https://ntfy.sh/secret-topic?token=fake-token-value'")
    )

    assert "secret-topic" not in text
    assert "fake-token-value" not in text
    assert "token=***" in text


def test_webhook_dispatch_error_redacts_discord_token(session, monkeypatch) -> None:
    webhook = models.WebhookConfig(
        url=_DISCORD_URL,
        name="discord-test",
        event_types=["high_signal_scan"],
        enabled=True,
    )
    session.add(webhook)
    session.flush()

    monkeypatch.setattr(webhook_mod, "validate_webhook_url", lambda url: url)
    monkeypatch.setattr(webhook_mod.httpx, "Client", _FailingClient)

    result = dispatch_webhook(session, webhook, "high_signal_scan", {"event_type": "x"})
    session.commit()

    assert result.success is False
    assert _DISCORD_TOKEN not in (result.error or "")
    dispatch = session.query(models.WebhookDispatch).one()
    assert _DISCORD_TOKEN not in (dispatch.error_message or "")
