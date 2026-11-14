from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import sessionmaker

from common.config import get_settings
from storage import models
from storage.repository import queued_write
from storage.write_queue import start_write_queue, stop_write_queue
from trading.errors import LiveDisabled
from trading.live_gate import (
    ARM_PHRASE,
    daily_loss_sol,
    effective_risk_pct,
    is_open,
    require_open,
)

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def queue(session):
    factory = sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)
    start_write_queue(session_factory=factory)
    yield
    stop_write_queue()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("LIVE_MAX_BANKROLL_SOL", "5")
    monkeypatch.setenv("LIVE_DAILY_LOSS_LIMIT_SOL", "1")
    monkeypatch.setenv("LIVE_MAX_RISK_PCT", "3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _gate(session, kind: str, ts: datetime = NOW) -> None:
    queued_write(lambda s: s.add(models.LiveGateState(ts=ts, kind=kind, reason="test")))


def test_phrase_value() -> None:
    assert ARM_PHRASE == "ENABLE LIVE TRADING"


def test_unknown_is_closed(session) -> None:
    assert is_open(session) is False
    with pytest.raises(LiveDisabled):
        require_open(session)


def test_env_flag_off_wins(session, queue, configured, monkeypatch) -> None:
    _gate(session, "armed")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "0")
    get_settings.cache_clear()
    assert is_open(session) is False
    with pytest.raises(LiveDisabled) as exc:
        require_open(session)
    assert exc.value.detail == "env_flag_off"


def test_arm_opens_and_kill_closes(session, queue, configured) -> None:
    _gate(session, "armed")
    require_open(session)

    _gate(session, "killed", ts=NOW + timedelta(minutes=1))
    assert is_open(session) is False


def test_rearm_after_kill(session, queue, configured) -> None:
    _gate(session, "armed")
    _gate(session, "killed", ts=NOW + timedelta(minutes=1))
    _gate(session, "armed", ts=NOW + timedelta(minutes=2))
    assert is_open(session) is True


def test_risk_pct_hard_cap(configured) -> None:
    assert effective_risk_pct() == Decimal("1")


def test_daily_loss_is_positive_sum(session, configured) -> None:
    session.add(
        models.LedgerTrade(
            mode="live",
            opened_at=NOW,
            closed_at=NOW,
            mint="X",
            chain="solana",
            source_signal="t",
            entry_price=Decimal("1"),
            size_sol=Decimal("1"),
            fees_sol=Decimal("0"),
            slippage_sol=Decimal("0"),
            exit_price=Decimal("1"),
            pnl_sol=Decimal("-0.4"),
            why_entered="t",
            why_exited="t",
            lesson="t",
            candidate_evidence_ts=NOW,
            price_source="fake",
            price_source_ts=NOW,
            rules_sha256="a",
            created_at=NOW,
        )
    )
    session.commit()
    assert daily_loss_sol(session, NOW) == Decimal("0.4")
    assert daily_loss_sol(session, NOW + timedelta(days=1)) == Decimal("0")
