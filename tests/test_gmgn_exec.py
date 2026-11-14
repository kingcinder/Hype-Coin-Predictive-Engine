from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from common.config import get_settings
from storage import models
from storage.repository import queued_write
from storage.write_queue import start_write_queue, stop_write_queue
from tests.conftest import seed_market_asset
from trading.errors import LiveDisabled
from trading.gmgn_exec import GmgnExecutor

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def queue(session):
    factory = sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)
    start_write_queue(session_factory=factory)
    yield
    stop_write_queue()


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("LIVE_MAX_BANKROLL_SOL", "5")
    monkeypatch.setenv("LIVE_DAILY_LOSS_LIMIT_SOL", "1")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _arm(session) -> None:
    queued_write(lambda s: s.add(models.LiveGateState(ts=NOW, kind="armed", reason="test")))


def _venue(mp) -> None:
    settings = get_settings()
    mp.setattr(settings, "gmgn_api_base", "https://gmgn.test")
    mp.setattr(settings, "gmgn_wallet_address", "wallet")
    mp.setattr(settings, "gmgn_signer_secret", "secret")


class FakeHttp:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or [])
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []

    def post_json(self, path, *, json=None):
        if path == "/order":
            self.submitted.append(dict(json or {}))
            return {"order_id": "order-1"}
        if path == "/order/cancel":
            self.cancelled.append((json or {}).get("order_id"))
            return {"cancelled": True}
        raise AssertionError(path)

    def get_json(self, path, *, params=None):
        if self.statuses:
            return self.statuses.pop(0)
        return {"status": "submitted"}


def _executor(session, http):
    return GmgnExecutor(session, http, poll_interval=0, fill_timeout=1)


def test_gate_closed_blocks(session, queue) -> None:
    with pytest.raises(LiveDisabled):
        _executor(session, FakeHttp()).buy(
            mint="mint",
            chain="solana",
            size_sol=Decimal("1"),
            slippage_pct=Decimal("10"),
            priority_fee_sol=Decimal("0.001"),
        )


def test_unconfigured_venue(session, queue, live_env) -> None:
    _arm(session)
    with pytest.raises(LiveDisabled) as exc:
        _executor(session, FakeHttp()).buy(
            mint="mint",
            chain="solana",
            size_sol=Decimal("1"),
            slippage_pct=Decimal("10"),
            priority_fee_sol=Decimal("0.001"),
        )
    assert exc.value.detail == "venue_unconfigured"


def test_filled_buy_records_fill(session, queue, live_env) -> None:
    _arm(session)
    _venue(live_env)
    http = FakeHttp([{"status": "filled", "price": "0.5", "size_sol": "1", "fee_sol": "0.01"}])

    fill = _executor(session, http).buy(
        mint="mint",
        chain="solana",
        size_sol=Decimal("1"),
        slippage_pct=Decimal("10"),
        priority_fee_sol=Decimal("0.001"),
    )

    assert fill.status == "filled"
    assert fill.price == Decimal("0.5")
    assert http.submitted[0]["side"] == "buy"
    session.expire_all()
    row = session.scalar(select(models.LiveFill))
    assert row is not None and row.status == "filled" and row.order_id == "order-1"


def test_unconfirmed_fill_raises_alert(session, queue, live_env) -> None:
    _arm(session)
    _venue(live_env)
    asset = seed_market_asset(session)
    session.commit()
    http = FakeHttp([{"status": "submitted"}])

    fill = _executor(session, http).buy(
        mint=asset.address,
        chain="solana",
        size_sol=Decimal("1"),
        slippage_pct=Decimal("10"),
        priority_fee_sol=Decimal("0.001"),
    )

    assert fill.status == "unconfirmed"
    session.expire_all()
    alerts = session.scalars(select(models.Alert)).all()
    assert [a.alert_type for a in alerts] == ["live_fill_unconfirmed"]


def test_cancel_calls_venue(session, queue, live_env) -> None:
    _arm(session)
    _venue(live_env)
    http = FakeHttp()
    assert _executor(session, http).cancel("order-9") is True
    assert http.cancelled == ["order-9"]
