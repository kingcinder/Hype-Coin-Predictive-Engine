from __future__ import annotations

import httpx
import pytest
from tenacity import retry, stop_after_attempt

import common.http as httpmod
from common.http import HttpClient, jittered_backoff


def test_jittered_backoff_prefers_new_tenacity_kwargs(monkeypatch) -> None:
    """R3 P2: on tenacity versions that accept wait_initial/wait_max, the new
    keyword shape is used and the result is passed through unchanged."""
    calls: list[dict] = []
    sentinel = object()

    def fake_factory(**kwargs):
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(httpmod, "wait_exponential_jitter", fake_factory)
    assert jittered_backoff(initial=0.5, maximum=8.0) is sentinel
    assert calls == [{"wait_initial": 0.5, "wait_max": 8.0}]


def test_jittered_backoff_falls_back_to_old_tenacity_kwargs(monkeypatch) -> None:
    """R3 P2: on tenacity versions that reject the new keywords (the locked
    9.1.4 only accepts the old ones), the old initial/max shape is used."""
    calls: list[dict] = []
    sentinel = object()

    def fake_factory(**kwargs):
        calls.append(kwargs)
        if "wait_initial" in kwargs:
            raise TypeError("unexpected keyword argument wait_initial")
        return sentinel

    monkeypatch.setattr(httpmod, "wait_exponential_jitter", fake_factory)
    assert jittered_backoff(initial=0.5, maximum=8.0) is sentinel
    assert calls == [{"wait_initial": 0.5, "wait_max": 8.0}, {"initial": 0.5, "max": 8.0}]


def test_jittered_backoff_works_with_installed_tenacity(monkeypatch) -> None:
    """R3 P2: the strategy the installed tenacity actually receives is a
    working wait - a retried function sleeps by it and then succeeds."""
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    attempts = {"count": 0}

    @retry(
        wait=jittered_backoff(initial=0.01, maximum=0.05), stop=stop_after_attempt(3), reraise=True
    )
    def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise ValueError("boom")
        return "ok"

    assert flaky() == "ok"
    assert attempts["count"] == 2


def _status_attempts(monkeypatch, status: int) -> int:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, request=request, json={"success": False})

    monkeypatch.setattr("tenacity.nap.sleep", lambda _seconds: None)
    transport = httpx.MockTransport(handler)
    client = HttpClient(base_url="https://example.test")
    client.close()
    client._client = httpx.Client(base_url="https://example.test", transport=transport)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_json("/probe")
    finally:
        client.close()
    return attempts


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_4xx_responses_are_not_retried(monkeypatch, status: int) -> None:
    assert _status_attempts(monkeypatch, status) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_429_and_5xx_responses_are_retried(monkeypatch, status: int) -> None:
    assert _status_attempts(monkeypatch, status) == 3


def test_429_retries_honor_retry_after_header(monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "7"},
            json={"success": False},
        )

    transport = httpx.MockTransport(handler)
    client = HttpClient(base_url="https://example.test")
    client.close()
    client._client = httpx.Client(base_url="https://example.test", transport=transport)
    monkeypatch.setattr(client._request_json.retry, "sleep", sleeps.append)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_json("/probe")
    finally:
        client.close()

    assert attempts == 3
    assert sleeps == [7.0, 7.0]


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_timeouts_and_transport_errors_are_retried(monkeypatch, exc_type) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise exc_type("probe failure", request=request)

    monkeypatch.setattr("tenacity.nap.sleep", lambda _seconds: None)
    transport = httpx.MockTransport(handler)
    client = HttpClient(base_url="https://example.test")
    client.close()
    client._client = httpx.Client(base_url="https://example.test", transport=transport)
    try:
        with pytest.raises(exc_type):
            client.get_json("/probe")
    finally:
        client.close()
    assert attempts == 3
