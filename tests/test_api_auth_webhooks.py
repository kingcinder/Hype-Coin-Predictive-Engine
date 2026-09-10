"""Regression tests for the H4/H5/H6 security fixes.

H4: every API endpoint requires ``Authorization: Bearer <ENGINE_API_TOKEN>``.
H5: the webhook signing secret is accepted via the ``X-Webhook-Secret``
    header only — a ``webhook_secret`` query parameter is ignored.
H6: webhook target URLs are validated against the SSRF policy on
    registration and again at dispatch time.
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

from api.main import app
from common.config import get_settings
from data_lake import webhooks as webhook_mod
from data_lake.webhooks import (
    WebhookURLError,
    dispatch_webhook,
    register_webhook,
    validate_webhook_url,
)
from storage import models
from storage.database import get_session

_TEST_API_TOKEN = "test-api-token-not-a-secret"
_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_API_TOKEN}"}
# Literal public IP (example.com) — getaddrinfo needs no DNS for literals.
_PUBLIC_URL = "http://93.184.216.34/hook"


@pytest.fixture(autouse=True)
def _api_auth_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENGINE_API_TOKEN", _TEST_API_TOKEN)
    monkeypatch.delenv("ENGINE_API_AUTH_BYPASS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _authed_client(**kwargs):
    headers = dict(_AUTH_HEADERS)
    headers.update(kwargs.pop("headers", {}))
    return TestClient(app, headers=headers, **kwargs)


# ── H4: bearer-token gate ─────────────────────────────────────────────────


def test_api_rejects_missing_token() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 401
    assert "Authorization" in response.json()["detail"]


def test_api_rejects_wrong_token() -> None:
    client = TestClient(app, headers={"Authorization": "Bearer wrong-token"})
    assert client.get("/health").status_code == 401


def test_api_rejects_malformed_scheme() -> None:
    client = TestClient(app, headers={"Authorization": "Token abc"})
    assert client.get("/health").status_code == 401


def test_api_rejects_on_mutating_endpoints_without_token() -> None:
    client = TestClient(app)
    assert client.post("/webhooks/register").status_code == 401
    assert client.get("/webhooks").status_code == 401


def test_api_fail_closed_when_token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ENGINE_API_TOKEN and no bypass -> 403 on every request, even valid ones."""
    monkeypatch.delenv("ENGINE_API_TOKEN", raising=False)
    monkeypatch.delenv("ENGINE_API_AUTH_BYPASS", raising=False)
    get_settings.cache_clear()
    try:
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 403
        assert "ENGINE_API_TOKEN" in response.json()["detail"]
        # A *valid-looking* bearer token still fails: the server is misconfigured.
        authed = TestClient(app, headers=_AUTH_HEADERS)
        assert authed.get("/health").status_code == 403
    finally:
        get_settings.cache_clear()


def test_api_bypass_flag_allows_unauthenticated(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENGINE_API_TOKEN", raising=False)
    monkeypatch.setenv("ENGINE_API_AUTH_BYPASS", "1")
    get_settings.cache_clear()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        assert client.get("/health").status_code == 200
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_websocket_handshake_rejected_without_token() -> None:
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/prices"):
            pass
    assert exc_info.value.code == 4401


def test_websocket_handshake_accepted_with_token() -> None:
    client = _authed_client()
    with client.websocket_connect("/ws/prices") as ws:
        # Immediately closing is fine — the handshake itself is the assertion.
        ws.close()


# ── H5: secret via header, not query param ────────────────────────────────


def _override_db(session):
    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session


def test_webhook_secret_accepted_via_header(session) -> None:
    _override_db(session)
    try:
        client = _authed_client()
        response = client.post(
            "/webhooks/register",
            params={"webhook_url": _PUBLIC_URL, "webhook_name": "sig-test"},
            headers={"X-Webhook-Secret": "s3cr3t"},
        )
        assert response.status_code == 200, response.text
        webhook_id = response.json()["id"]
        stored = session.get(models.WebhookConfig, webhook_id)
        assert stored is not None
        assert stored.secret == "s3cr3t"
    finally:
        app.dependency_overrides.clear()


def test_webhook_secret_query_param_is_ignored(session) -> None:
    """The old query-param vector must not populate the HMAC secret."""
    _override_db(session)
    try:
        client = _authed_client()
        response = client.post(
            "/webhooks/register",
            params={
                "webhook_url": _PUBLIC_URL,
                "webhook_name": "query-secret-test",
                "webhook_secret": "leaked-in-query",
            },
        )
        assert response.status_code == 200, response.text
        stored = session.get(models.WebhookConfig, response.json()["id"])
        assert stored is not None
        assert stored.secret is None
    finally:
        app.dependency_overrides.clear()


# ── H6: SSRF validation ───────────────────────────────────────────────────


def test_register_rejects_metadata_service_url(session) -> None:
    _override_db(session)
    try:
        client = _authed_client()
        response = client.post(
            "/webhooks/register",
            params={
                "webhook_url": "http://169.254.169.254/latest/meta-data/iam",
                "webhook_name": "ssrf",
            },
        )
        assert response.status_code == 400
        assert "non-public IP" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_register_rejects_loopback_url(session) -> None:
    _override_db(session)
    try:
        client = _authed_client()
        response = client.post(
            "/webhooks/register",
            params={"webhook_url": "http://127.0.0.1:8080/hook", "webhook_name": "loop"},
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_register_rejects_non_http_scheme(session) -> None:
    _override_db(session)
    try:
        client = _authed_client()
        response = client.post(
            "/webhooks/register",
            params={"webhook_url": "ftp://example.com/hook", "webhook_name": "ftp"},
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_register_rejects_credentials_in_url(session) -> None:
    _override_db(session)
    try:
        client = _authed_client()
        response = client.post(
            "/webhooks/register",
            params={
                "webhook_url": "https://user:pass@93.184.216.34/hook",
                "webhook_name": "creds",
            },
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_validate_webhook_url_blocks_resolved_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname whose DNS resolves to RFC1918 is blocked (DNS-rebinding safe)."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "evil.example"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0)),
        ]

    monkeypatch.setattr(webhook_mod.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(WebhookURLError, match="non-public IP"):
        validate_webhook_url("https://evil.example/hook")


def test_validate_webhook_url_allows_resolved_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(webhook_mod.socket, "getaddrinfo", fake_getaddrinfo)
    assert validate_webhook_url("https://cdn.example/hook") == "https://cdn.example/hook"


def test_validate_webhook_url_rejects_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(webhook_mod.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(WebhookURLError, match="does not resolve"):
        validate_webhook_url("https://nonexistent.invalid/hook")


def test_validate_webhook_url_allowlist_permits_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBHOOK_URL_ALLOWLIST_CSV", "hooks.internal, 10.9.9.9")
    get_settings.cache_clear()
    try:
        # No DNS involved: allowlisted hosts skip resolution entirely.
        assert (
            validate_webhook_url("http://hooks.internal:8080/hook")
            == "http://hooks.internal:8080/hook"
        )
        assert validate_webhook_url("http://10.9.9.9/hook") == "http://10.9.9.9/hook"
        # ...but the scheme/credential rules still apply to allowlisted hosts.
        with pytest.raises(WebhookURLError, match="http or https"):
            validate_webhook_url("ftp://hooks.internal/hook")
        with pytest.raises(WebhookURLError, match="credentials"):
            validate_webhook_url("http://u:p@hooks.internal/hook")
        with pytest.raises(WebhookURLError, match="non-public IP"):
            validate_webhook_url("http://127.0.0.1/hook")
    finally:
        get_settings.cache_clear()


def test_validate_webhook_url_private_hosts_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_HOSTS", "1")
    get_settings.cache_clear()
    try:
        assert validate_webhook_url("http://127.0.0.1:8080/hook") == "http://127.0.0.1:8080/hook"
    finally:
        get_settings.cache_clear()


def test_register_webhook_function_validates_url(session) -> None:
    """Direct callers of register_webhook get the same SSRF policy."""
    with pytest.raises(WebhookURLError):
        register_webhook(
            session, url="http://169.254.169.254/", name="x", secret=None
        )


def test_dispatch_webhook_blocks_ssrf_url_at_send_time(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing row pointing at an internal target is never POSTed to."""

    def _no_http(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("dispatch must not issue HTTP for a blocked URL")

    monkeypatch.setattr(webhook_mod.httpx, "Client", _no_http)
    webhook = models.WebhookConfig(
        url="http://169.254.169.254/latest/meta-data/",
        name="legacy-ssrf",
        event_types=["high_signal_scan"],
        enabled=True,
    )
    session.add(webhook)
    session.flush()

    result = dispatch_webhook(session, webhook, "high_signal_scan", {"event_type": "x"})
    assert result.success is False
    assert result.status_code is None
    assert "blocked" in (result.error or "")


class _CapturingHttpClient:
    """Stand-in for httpx.Client: captures the dispatched request instead of
    sending it. Kept here (not shared) because dispatch_webhook constructs the
    client inline."""

    instances: list["_CapturingHttpClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.posts: list[dict] = []
        _CapturingHttpClient.instances.append(self)

    def __enter__(self) -> "_CapturingHttpClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def post(self, url, content=None, headers=None):
        self.posts.append({"url": url, "content": content, "headers": dict(headers or {})})

        class _Resp:
            status_code = 200

        return _Resp()


def _dispatch_with_capture(
    session, monkeypatch: pytest.MonkeyPatch, webhook: "models.WebhookConfig"
) -> dict:
    """Run dispatch_webhook with a stubbed URL policy + HTTP client; return
    the captured POST."""
    _CapturingHttpClient.instances.clear()
    monkeypatch.setattr(webhook_mod, "validate_webhook_url", lambda url: url)
    monkeypatch.setattr(webhook_mod.httpx, "Client", _CapturingHttpClient)
    result = dispatch_webhook(session, webhook, "high_signal_scan", {"event_type": "x"})
    assert result.success is True
    assert len(_CapturingHttpClient.instances) == 1
    assert len(_CapturingHttpClient.instances[0].posts) == 1
    return _CapturingHttpClient.instances[0].posts[0]


def test_dispatch_webhook_signature_covers_transmitted_body(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H5 end-to-end: X-Signature-256 must equal the HMAC-SHA256 of the exact
    bytes POSTed, so a receiver can verify the body it actually got."""
    import hashlib
    import hmac

    secret = "s3cr3t-signing-key"
    webhook = models.WebhookConfig(
        url="https://hooks.example.com/hook",
        name="sig-check",
        event_types=["high_signal_scan"],
        enabled=True,
        secret=secret,
    )
    session.add(webhook)
    session.flush()

    post = _dispatch_with_capture(session, monkeypatch, webhook)
    expected = "sha256=" + hmac.new(
        secret.encode(), post["content"], hashlib.sha256
    ).hexdigest()
    assert post["headers"].get("X-Signature-256") == expected


def test_dispatch_webhook_signature_covers_reformatted_telegram_body(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Telegram/Discord reformatting must happen BEFORE signing: the
    signature has to cover the reformatted body, not the generic payload."""
    import hashlib
    import hmac
    import json

    secret = "s3cr3t-signing-key"
    webhook = models.WebhookConfig(
        url="https://api.telegram.org/botTOKEN/sendMessage",
        name="12345",  # chat_id for Telegram targets
        event_types=["high_signal_scan"],
        enabled=True,
        secret=secret,
    )
    session.add(webhook)
    session.flush()

    post = _dispatch_with_capture(session, monkeypatch, webhook)
    body = json.loads(post["content"])
    # The transmitted body is the Telegram reformatting ...
    assert body["chat_id"] == "12345"
    assert "text" in body
    # ... and the signature verifies against THOSE bytes, not the generic
    # payload (this failed before the sign-after-reformat fix).
    expected = "sha256=" + hmac.new(
        secret.encode(), post["content"], hashlib.sha256
    ).hexdigest()
    assert post["headers"].get("X-Signature-256") == expected


def test_dispatch_webhook_no_signature_header_without_secret(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A webhook with no registered secret sends no signature header."""
    webhook = models.WebhookConfig(
        url="https://hooks.example.com/hook",
        name="no-secret",
        event_types=["high_signal_scan"],
        enabled=True,
        secret=None,
    )
    session.add(webhook)
    session.flush()

    post = _dispatch_with_capture(session, monkeypatch, webhook)
    assert "X-Signature-256" not in post["headers"]
