from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from common.config import get_settings
from storage import models
from storage.write_queue import start_write_queue, stop_write_queue
from tests.conftest import seed_market_asset
from trading import copy
from trading.errors import TradeRefused

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
def queue(session):
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    start_write_queue(session_factory=factory)
    try:
        yield
    finally:
        stop_write_queue()


@pytest.fixture()
def enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "copy_trading_enabled", True)
    monkeypatch.setattr(settings, "copy_requires_confirmation", True)
    monkeypatch.setattr(copy, "load_wallets", lambda: [{"address": "W", "chain": "solana"}])


def _rows(session, model):
    session.expire_all()
    return session.scalars(select(model).order_by(model.id)).all()


def test_disabled_writes_nothing(session, queue, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "copy_trading_enabled", False)
    assert copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW) is None
    assert _rows(session, models.CopyLog) == []


def test_closed_gate_is_paper_only(session, queue, enabled) -> None:
    seed_market_asset(session)
    row = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert row is not None
    rows = _rows(session, models.CopyLog)
    assert len(rows) == 1
    assert rows[0].status == "paper_only"
    assert rows[0].size_sol == Decimal("0.1")
    assert _rows(session, models.LedgerOpenPosition) == []


def test_open_gate_queues_pending_and_approve_delay(session, queue, enabled, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=NOW, kind="armed", reason="test"))
    session.commit()
    seed_market_asset(session)

    row = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert row is not None
    pending = _rows(session, models.CopyLog)[0]
    assert pending.status == "pending"
    assert pending.size_sol == Decimal("0.1")
    assert _rows(session, models.LedgerOpenPosition) == []

    refused = copy.approve_copy(session, pending.id, now=NOW)
    assert refused.status == "refused"
    assert refused.detail == "copy_delay_not_elapsed"

    session.add(models.LiveGateState(ts=NOW, kind="killed", reason="test"))
    session.commit()
    killed = copy.approve_copy(session, pending.id, now=NOW)
    assert killed.status == "refused"


def test_second_approve_is_idempotent(session, queue, enabled, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=NOW, kind="armed", reason="test"))
    session.commit()
    seed_market_asset(session)
    pending = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert pending is not None

    opened: list[int] = []

    class FakeEngine:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def open_trade(self, *args, **kwargs) -> int:
            opened.append(1)
            return 99

    monkeypatch.setattr(copy, "get_candidate", lambda *args, **kwargs: {"mint": "M"})
    monkeypatch.setattr("trading.ledger.TradeEngine", FakeEngine)

    first = copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=60))
    second = copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=120))

    assert first.id == second.id == pending.id
    assert first.status == second.status == "executed"
    assert first.detail == second.detail == "position_id=99"
    assert len(opened) == 1
    assert len(_rows(session, models.CopyLog)) == 1
    assert _rows(session, models.LedgerOpenPosition) == []


def test_overlapping_approve_cannot_open_twice(session, queue, enabled, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=NOW, kind="armed", reason="test"))
    session.commit()
    seed_market_asset(session)
    pending = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert pending is not None

    opened: list[int] = []
    refused: list[str] = []

    class FakeEngine:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def open_trade(self, *args, **kwargs) -> int:
            opened.append(1)
            try:
                copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=90))
            except TradeRefused as exc:
                refused.append(exc.reason)
            return 99

    monkeypatch.setattr(copy, "get_candidate", lambda *args, **kwargs: {"mint": "M"})
    monkeypatch.setattr("trading.ledger.TradeEngine", FakeEngine)

    result = copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=60))

    assert result.status == "executed"
    assert len(opened) == 1
    assert refused == ["copy_approval_in_progress"]
    assert len(_rows(session, models.CopyLog)) == 1
    assert _rows(session, models.LedgerOpenPosition) == []
