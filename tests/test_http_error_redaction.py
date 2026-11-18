"""HTTP failures must not leak provider credentials embedded in request URLs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import structlog
from sqlalchemy import select

import crawlers.base as base_mod
import crawlers.sources.cmc as cmc_mod
import ingestion.service as ingestion_service
import narrative.engine as narrative_mod
from common.config import get_settings
from common.logging import configure_logging, get_logger
from crawlers.base import BaseCrawler
from crawlers.sources.cmc import CMCCrawler
from ingestion.service import IngestionService
from narrative.engine import NarrativeEngine
from storage import models
from tests.conftest import seed_market_asset

SECRET = "00000000-0000-4000-8000-000000000001"
HELIUS = f"https://mainnet.helius-rpc.com/?api-key={SECRET}"
HTTP_ERROR = f"Client error for url '{HELIUS}'"


class _CaptureLog:
    def __init__(self) -> None:
        self.event = ""
        self.fields: dict[str, object] = {}

    def _record(self, event: str, **kwargs: object) -> None:
        self.event = event
        self.fields = kwargs

    def debug(self, event: str, **kwargs: object) -> None:
        self._record(event, **kwargs)

    def info(self, event: str, **kwargs: object) -> None:
        self._record(event, **kwargs)

    def warning(self, event: str, **kwargs: object) -> None:
        self._record(event, **kwargs)

    def exception(self, event: str, **kwargs: object) -> None:
        self._record(event, **kwargs)


def _assert_redacted(value: object) -> None:
    assert SECRET not in str(value)
    assert "api-key=***" in str(value)


def test_base_crawler_redacts_http_error_in_log_and_health(monkeypatch) -> None:
    class _FailingCrawler(BaseCrawler):
        def fetch_items(self):  # pragma: no cover - called through fetch()
            raise RuntimeError(HTTP_ERROR)

    crawler = _FailingCrawler(
        name="redaction_probe",
        max_retries=0,
        rate_limit_pause=0.0,
    )
    crawler._client = SimpleNamespace(is_closed=False)  # type: ignore[assignment]
    captured = _CaptureLog()
    monkeypatch.setattr(base_mod, "log", captured)

    assert crawler.fetch() == []

    assert captured.event == "crawler_error"
    _assert_redacted(captured.fields["error"])
    _assert_redacted(crawler.health.last_error)


def test_cmc_http_failure_log_redacts_url_key(monkeypatch, capfd) -> None:
    cmc_url = (
        f"https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest?api-key={SECRET}"
    )
    cmc_error = f"Server error '500 Internal Server Error' for url '{cmc_url}'"

    class _FailingHttp:
        is_closed = False

        def get(self, *_args, **_kwargs):
            raise RuntimeError(cmc_error)

    crawler = CMCCrawler(api_key=SECRET)
    crawler._client = _FailingHttp()  # type: ignore[assignment]

    # CMC records this failure at DEBUG. Reconfigure the real logger long enough
    # to capture its actual output, rather than substituting a fake log object.
    old_config = structlog.get_config()
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    get_settings.cache_clear()
    configure_logging()
    monkeypatch.setattr(cmc_mod, "log", get_logger("crawlers.sources.cmc"))
    try:
        assert crawler._fetch_pro_items() == []
        captured = capfd.readouterr()
        logs = captured.out + captured.err
    finally:
        get_settings.cache_clear()
        structlog.configure(**old_config)

    assert "cmc_pro_listings_failed" in logs
    _assert_redacted(logs)


def test_dexscreener_ingestion_redacts_http_error_in_log_and_health(session, monkeypatch) -> None:
    class _FailingDexScreener:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def latest_token_profiles(self):
            raise RuntimeError(HTTP_ERROR)

        def close(self) -> None:
            pass

    monkeypatch.setattr(ingestion_service, "DexScreenerClient", _FailingDexScreener)
    captured = _CaptureLog()
    monkeypatch.setattr(ingestion_service, "log", captured)

    service = IngestionService()
    service.ensure_reference_data(session)
    assert service._ingest_dexscreener_profiles(session) == 0
    session.commit()

    assert captured.event == "dexscreener_profiles_failed"
    _assert_redacted(captured.fields["error"])
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "source:dexscreener_profiles")
        .order_by(models.SystemHealth.id.desc())
    )
    assert health is not None
    _assert_redacted(health.message)


def test_narrative_crawl_failure_redacts_http_error_in_log_and_health(session, monkeypatch) -> None:
    class _FailingCrawler:
        def fetch(self):
            raise RuntimeError(HTTP_ERROR)

    seed_market_asset(session)
    captured = _CaptureLog()
    monkeypatch.setattr(narrative_mod, "log", captured)
    engine = NarrativeEngine()

    count = engine._crawl_social(
        session,
        crawler=_FailingCrawler(),
        source_name="reddit_public",
        source_type="social",
        base_url="https://www.reddit.com",
        endpoint_pool=None,
        endpoint_url=None,
        decision_ts=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )
    session.commit()

    assert count == 0
    assert captured.event == "narrative_crawl_failed"
    _assert_redacted(captured.fields["error"])
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "source:reddit_public")
        .order_by(models.SystemHealth.id.desc())
    )
    assert health is not None
    _assert_redacted(health.message)


def test_rss_crawl_failure_redacts_feed_url_credentials(session, monkeypatch) -> None:
    class _FailingRss:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def fetch(self):
            raise RuntimeError(HTTP_ERROR)

        def close(self) -> None:
            pass

    monkeypatch.setattr(narrative_mod, "RSSNewsCrawler", _FailingRss)
    captured = _CaptureLog()
    monkeypatch.setattr(narrative_mod, "log", captured)
    engine = NarrativeEngine()
    monkeypatch.setattr(engine.settings, "rss_feed_urls_csv", "https://news.example/feed")

    assert engine._crawl_rss(session, decision_ts=datetime(2026, 5, 1, 12, 0, tzinfo=UTC)) == 0
    session.commit()

    assert captured.event == "rss_crawl_failed"
    _assert_redacted(captured.fields["error"])
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "source:rss_news")
        .order_by(models.SystemHealth.id.desc())
    )
    assert health is not None
    _assert_redacted(health.message)
