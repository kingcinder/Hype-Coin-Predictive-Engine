from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from api import trading_routes
from common.config import get_settings
from storage import models
from storage.write_queue import start_write_queue, stop_write_queue
from trading.live_gate import is_open

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def queue(session):
    factory = sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)
    start_write_queue(session_factory=factory)
    yield
    stop_write_queue()


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fill(session, status="submitted"):
    session.add(
        models.LiveFill(
            order_id=f"order-{status}",
            mint="mint",
            chain="solana",
            side="buy",
            ts=NOW,
            price=Decimal("1"),
            size_sol=Decimal("1"),
            fee_sol=Decimal("0"),
            slippage_pct=Decimal("10"),
            tx_signature=None,
            status=status,
            ledger_ref=None,
        )
    )
    session.commit()


def test_kill_works_with_flag_off(session, queue) -> None:
    assert is_open(session) is False

    result = asyncio.run(trading_routes.trading_kill(session))

    assert result["killed"] is True
    session.expire_all()
    kinds = session.scalars(select(models.LiveGateState.kind)).all()
    assert "killed" in kinds
    assert is_open(session) is False


def test_kill_cancels_submitted_orders(session, queue, monkeypatch) -> None:
    _fill(session, "submitted")
    _fill(session, "filled")
    calls: list[str] = []

    class FakeExecutor:
        def __init__(self, _session):
            pass

        def cancel(self, order_id):
            calls.append(order_id)
            return True

    monkeypatch.setattr("trading.gmgn_exec.GmgnExecutor", FakeExecutor)

    result = asyncio.run(trading_routes.trading_kill(session))

    assert calls == ["order-submitted"]
    assert result["cancelled"] == 1
    assert result["failed"] == 0


def test_kill_survives_executor_errors(session, queue, monkeypatch) -> None:
    _fill(session, "submitted")

    class BoomExecutor:
        def __init__(self, _session):
            pass

        def cancel(self, order_id):
            raise RuntimeError("venue down")

    monkeypatch.setattr("trading.gmgn_exec.GmgnExecutor", BoomExecutor)

    result = asyncio.run(trading_routes.trading_kill(session))

    assert result == {"killed": True, "cancelled": 0, "failed": 1}
