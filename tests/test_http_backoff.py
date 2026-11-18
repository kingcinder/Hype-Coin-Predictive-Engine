from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from tenacity import retry, stop_after_attempt

import common.http as httpmod
from common.http import (
    MAX_RETRY_AFTER_SECONDS,
    HttpClient,
    jittered_backoff,
    retry_after_seconds,
)


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


def _retry_after_header_error(headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/probe")
    response = httpx.Response(429, request=request, headers=headers or {})
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


def test_retry_after_parses_http_date() -> None:
    retry_at = datetime.now(UTC) + timedelta(seconds=60)
    seconds = retry_after_seconds(
        _retry_after_header_error({"Retry-After": format_datetime(retry_at, usegmt=True)})
    )
    assert seconds is not None
    assert 50 <= seconds <= 61


def test_retry_after_missing_header_returns_none() -> None:
    assert retry_after_seconds(_retry_after_header_error()) is None


@pytest.mark.parametrize("value", ["0", "-1"])
def test_retry_after_non_positive_returns_none(value: str) -> None:
    assert retry_after_seconds(_retry_after_header_error({"Retry-After": value})) is None


def test_retry_after_invalid_header_returns_none() -> None:
    assert retry_after_seconds(_retry_after_header_error({"Retry-After": "not-a-date"})) is None


def test_retry_after_caps_huge_header() -> None:
    assert (
        retry_after_seconds(_retry_after_header_error({"Retry-After": "999999999"}))
        == MAX_RETRY_AFTER_SECONDS
    )


def test_retry_after_caps_huge_http_date() -> None:
    retry_at = datetime.now(UTC) + timedelta(days=30)
    seconds = retry_after_seconds(
        _retry_after_header_error({"Retry-After": format_datetime(retry_at, usegmt=True)})
    )
    assert seconds == MAX_RETRY_AFTER_SECONDS


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


def test_429_retry_wait_caps_huge_retry_after(monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "999999999"},
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
    # The normal exponential-backoff ceiling still bounds the wait after the
    # header parser's own cap.
    assert sleeps == [8.0, 8.0]


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
