"""Regression tests for crawler-orchestrator fixes (C3, H26).

C3: ``NightCrawlerOrchestrator.run_all`` used a plain ``threading.Lock`` while
``_should_run`` re-acquired the same lock — a scheduled (``force=False``) run
deadlocked itself.

H26: a source crawler whose HTTP client construction failed (e.g. malformed
``NO_PROXY`` in the environment) used to return ``[]`` and record a *success*;
empty results no longer count as success and construction errors are surfaced
as errors.
"""

from __future__ import annotations

import threading

import pytest

from crawlers.base import BaseCrawler
from crawlers.orchestrator import NightCrawlerOrchestrator


class _EmptyCrawler(BaseCrawler):
    """Returns no items without touching the network."""

    base_url = "https://example.com"

    def __init__(self) -> None:
        super().__init__("test_empty", max_retries=0, rate_limit_pause=0.0)

    def fetch_items(self) -> list[dict]:
        return []


def _orchestrator_with(crawler: BaseCrawler) -> NightCrawlerOrchestrator:
    orch = NightCrawlerOrchestrator()
    orch._crawlers = {crawler.name: crawler}
    return orch


def test_run_all_force_false_does_not_deadlock(session) -> None:
    """C3: a scheduled run must complete even though _should_run re-locks."""
    orch = _orchestrator_with(_EmptyCrawler())
    outcome: dict = {}
    thread = threading.Thread(
        target=lambda: outcome.update(results=orch.run_all(session, force=False)),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "run_all(force=False) deadlocked on its own lock"
    details = outcome["results"]["details"]
    assert details["test_empty"]["status"] == "ok"
    assert details["test_empty"]["items"] == 0


def test_client_construction_failure_records_error(session, monkeypatch) -> None:
    """H26: if the HTTP client cannot be built, fetch() reports an error."""
    import crawlers.base

    def _broken(**kwargs):
        raise RuntimeError("invalid proxy environment")

    monkeypatch.setattr(crawlers.base, "build_httpx_client", _broken)
    crawler = _EmptyCrawler()
    items = crawler.fetch()
    assert items == []
    # fetch() tracks health in-memory on the crawler; the error must not be
    # swallowed into a bogus success.
    assert crawler.health.consecutive_errors >= 1
    assert crawler.health.last_error is not None
    assert "proxy environment" in crawler.health.last_error


def test_repeated_empty_runs_degrade_health_without_tripping_gate(session) -> None:
    """H26: empty runs reduce reliability but do not mark the source unhealthy."""
    crawler = _EmptyCrawler()
    for _ in range(3):
        assert crawler.fetch() == []
    assert crawler.health.consecutive_empty_runs == 3
    assert crawler.health.reliability_score == pytest.approx(1.0 - 3 * 0.15)
    assert crawler.health.is_healthy  # degradation, not the hard unhealthy gate

    # a successful run resets the streak
    crawler.fetch_items = lambda: [{"title": "x"}]  # type: ignore[method-assign]
    assert crawler.fetch() != []
    assert crawler.health.consecutive_empty_runs == 0
