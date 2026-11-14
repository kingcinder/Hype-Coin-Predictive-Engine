from __future__ import annotations

from tenacity import retry, stop_after_attempt

import common.http as httpmod
from common.http import jittered_backoff


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
