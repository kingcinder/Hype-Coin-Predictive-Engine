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
from trading.errors import LiveDisabled
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


def test_kill_cancels_with_real_executor_after_gate_closes(session, queue, monkeypatch) -> None:
    """The kill switch must cancel through the real executor, then close the gate."""
    from storage.repository import queued_write
    from trading.gmgn_exec import GmgnExecutor

    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("LIVE_MAX_BANKROLL_SOL", "5")
    monkeypatch.setenv("LIVE_DAILY_LOSS_LIMIT_SOL", "1")
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "gmgn_api_base", "https://gmgn.test")
    monkeypatch.setattr(settings, "gmgn_wallet_address", "wallet")
    monkeypatch.setattr(settings, "gmgn_api_key", "ak")
    monkeypatch.setattr(settings, "gmgn_signer_secret", "sk")

    _fill(session, "submitted")
    _fill(session, "filled")
    queued_write(lambda s: s.add(models.LiveGateState(ts=NOW, kind="armed", reason="test")))

    calls: list[tuple[str, dict, dict]] = []

    class RecordingHttp:
        def __init__(self, *, base_url: str = "", timeout=None, headers=None) -> None:
            pass

        def post_json(self, path, *, json=None, headers=None):
            calls.append((path, dict(json or {}), dict(headers or {})))
            return {"cancelled": True}

        def get_json(self, path, *, params=None, headers=None):
            raise AssertionError(path)

    monkeypatch.setattr("trading.gmgn_exec.HttpClient", RecordingHttp)

    result = asyncio.run(trading_routes.trading_kill(session))

    assert result == {"killed": True, "cancelled": 1, "failed": 0, "uncancelled": []}
    assert [call[0] for call in calls] == ["/order/cancel"]
    assert calls[0][1] == {"order_id": "order-submitted"}
    assert calls[0][2]["X-Ak"] == "ak" and calls[0][2]["X-Signature"]

    with pytest.raises(LiveDisabled):
        GmgnExecutor(session).buy(
            mint="mint",
            chain="solana",
            size_sol=Decimal("1"),
            slippage_pct=Decimal("10"),
            priority_fee_sol=Decimal("0.001"),
        )


def test_kill_survives_executor_errors(session, queue, monkeypatch) -> None:
    _fill(session, "submitted")

    class BoomExecutor:
        def __init__(self, _session):
            pass

        def cancel(self, order_id):
            raise RuntimeError("venue down")

    monkeypatch.setattr("trading.gmgn_exec.GmgnExecutor", BoomExecutor)

    result = asyncio.run(trading_routes.trading_kill(session))

    assert result == {
        "killed": True,
        "cancelled": 0,
        "failed": 1,
        "uncancelled": ["order-submitted"],
    }
