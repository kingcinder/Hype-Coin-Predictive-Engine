from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from common.config import get_settings
from storage import models
from storage.write_queue import start_write_queue, stop_write_queue
from tests.conftest import seed_market_asset
from trading import copy
from trading.errors import TradeRefused
from trading.executors import PriceQuote

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
def rules_file(tmp_path, monkeypatch):
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps({"buy_trigger": "ignition_detected", "stop_pct": 20, "target_pct": 50})
    )
    monkeypatch.setattr(get_settings(), "trading_rules_path", str(path))
    return path


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


def test_double_approval_opens_one_position(
    session, queue, enabled, rules_file, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=NOW, kind="armed", reason="test"))
    session.commit()
    seed_market_asset(session)
    pending = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert pending is not None
    buys: list[str] = []

    class FakePriceSource:
        def price(self, mint: str, chain: str) -> PriceQuote:
            return PriceQuote(price=Decimal("1"), ts=NOW + timedelta(seconds=60), source="fake")

    class FakeExecutor:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def buy(self, **kwargs) -> SimpleNamespace:
            buys.append(kwargs["mint"])
            return SimpleNamespace(
                status="filled",
                price=Decimal("1"),
                size_sol=Decimal("0.1"),
                fee_sol=Decimal("0"),
            )

    monkeypatch.setattr(
        copy,
        "get_candidate",
        lambda *args, **kwargs: {
            "mint": "M",
            "chain": "solana",
            "evidence_ts": NOW,
            "age_minutes": 5,
            "signal_id": "copy:test",
        },
    )
    monkeypatch.setattr(
        copy, "MarketSnapshotPriceSource", lambda *args, **kwargs: FakePriceSource()
    )
    monkeypatch.setattr("trading.gmgn_exec.GmgnExecutor", FakeExecutor)

    first = copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=60))
    second = copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=120))

    assert first.status == "executed", first.detail
    assert second.status == "executed"
    assert first.detail == second.detail
    assert buys == ["M"]
    positions = _rows(session, models.LedgerOpenPosition)
    assert len(positions) == 1
    assert positions[0].mint == "M"
    assert positions[0].why_entered == "copy:W"


def test_executed_state_persists_for_fresh_session(session, queue, enabled, monkeypatch) -> None:
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
    assert first.status == "executed"

    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    with factory() as fresh:
        reloaded = fresh.get(models.CopyLog, pending.id)
        assert reloaded is not None
        assert reloaded.status == "executed"
        assert reloaded.detail == "position_id=99"
        second = copy.approve_copy(fresh, pending.id, now=NOW + timedelta(seconds=120))
        assert second.id == pending.id
        assert second.status == "executed"
        assert second.detail == "position_id=99"

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


def test_conditional_claim_update_succeeds_only_once(session) -> None:
    log = models.CopyLog(
        ts=NOW,
        wallet="W",
        mint="M",
        size_sol=Decimal("1"),
        status="pending",
        detail="awaiting approval",
    )
    session.add(log)
    session.commit()
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)

    with factory() as first, factory() as second:
        assert copy._claim_pending_copy(first, log.id) is True
        first.commit()
        assert copy._claim_pending_copy(second, log.id) is False
        second.rollback()

    session.expire_all()
    reloaded = session.get(models.CopyLog, log.id)
    assert reloaded is not None
    assert reloaded.status == "approving"
    assert reloaded.detail == "approval_claimed"


def test_racing_approvals_open_only_once(session, queue, enabled, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=NOW, kind="armed", reason="test"))
    session.commit()
    seed_market_asset(session)
    pending = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert pending is not None
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    barrier = Barrier(2)
    opened: list[int] = []

    class FakeEngine:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def open_trade(self, *args, **kwargs) -> int:
            opened.append(1)
            time.sleep(0.2)
            return 99

    monkeypatch.setattr(copy, "get_candidate", lambda *args, **kwargs: {"mint": "M"})
    monkeypatch.setattr("trading.ledger.TradeEngine", FakeEngine)

    def approve(offset: int):
        with factory() as db:
            barrier.wait(timeout=5)
            return copy.approve_copy(db, pending.id, now=NOW + timedelta(seconds=offset))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve, 60), pool.submit(approve, 90)]
        executed = []
        refused: list[str] = []
        for future in futures:
            try:
                executed.append(future.result(timeout=10))
            except TradeRefused as exc:
                refused.append(exc.reason)

    assert len(opened) == 1
    assert executed and all(row.status == "executed" for row in executed)
    assert set(refused) <= {"copy_approval_in_progress"}
    assert len(_rows(session, models.CopyLog)) == 1
    assert _rows(session, models.CopyLog)[0].status == "executed"
    assert _rows(session, models.LedgerOpenPosition) == []


def test_two_sessions_approve_same_log_open_one_live_trade(
    session, queue, enabled, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=NOW, kind="armed", reason="test"))
    session.commit()
    seed_market_asset(session)
    pending = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert pending is not None
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    barrier = Barrier(2)
    opened_modes: list[str] = []

    class LiveTradeProbe:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def open_trade(self, *args, **kwargs) -> int:
            opened_modes.append(kwargs["mode"])
            time.sleep(0.2)
            return 99

    monkeypatch.setattr(copy, "get_candidate", lambda *args, **kwargs: {"mint": "M"})
    monkeypatch.setattr("trading.ledger.TradeEngine", LiveTradeProbe)

    def approve(offset: int) -> str:
        with factory() as db:
            barrier.wait(timeout=5)
            try:
                return copy.approve_copy(db, pending.id, now=NOW + timedelta(seconds=offset)).status
            except TradeRefused as exc:
                return exc.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve, 60), pool.submit(approve, 90)]
        outcomes = [future.result(timeout=10) for future in futures]

    assert opened_modes == ["live"]
    assert outcomes.count("executed") >= 1
    assert set(outcomes) <= {"executed", "copy_approval_in_progress"}
    assert _rows(session, models.CopyLog)[0].status == "executed"


def test_unexpected_failure_keeps_claim(session, queue, enabled, monkeypatch) -> None:
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
            raise RuntimeError("venue response lost")

    monkeypatch.setattr(copy, "get_candidate", lambda *args, **kwargs: {"mint": "M"})
    monkeypatch.setattr("trading.ledger.TradeEngine", FakeEngine)

    with pytest.raises(RuntimeError):
        copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=60))

    held = _rows(session, models.CopyLog)[0]
    assert held.status == "approving"
    assert held.detail == "submission_started"
    with pytest.raises(TradeRefused, match="copy_approval_in_progress"):
        copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=120))
    assert len(opened) == 1
    assert _rows(session, models.LedgerOpenPosition) == []


@pytest.mark.parametrize("reason", sorted(copy._POST_SUBMIT_REFUSAL_REASONS))
def test_post_submit_refusal_keeps_claim_and_blocks_retry(
    session, queue, enabled, monkeypatch, reason
) -> None:
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
            raise TradeRefused(reason)

    monkeypatch.setattr(copy, "get_candidate", lambda *args, **kwargs: {"mint": "M"})
    monkeypatch.setattr("trading.ledger.TradeEngine", FakeEngine)

    with pytest.raises(TradeRefused, match=reason):
        copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=60))

    held = _rows(session, models.CopyLog)[0]
    assert held.status == "approving"
    assert held.detail == "submission_started"
    with pytest.raises(TradeRefused, match="copy_approval_in_progress"):
        copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=120))
    assert len(opened) == 1
    assert _rows(session, models.LedgerOpenPosition) == []


def test_recover_pre_submission_claim_allows_retry(session, queue, enabled, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "live_trading_enabled", "1")
    monkeypatch.setattr(settings, "live_max_bankroll_sol", Decimal("1"))
    monkeypatch.setattr(settings, "live_daily_loss_limit_sol", Decimal("1"))
    session.add(models.LiveGateState(ts=NOW, kind="armed", reason="test"))
    session.commit()
    seed_market_asset(session)
    pending = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert pending is not None
    log = _rows(session, models.CopyLog)[0]
    log.status = "approving"
    log.detail = "approval_claimed"
    session.commit()

    recovered = copy.recover_copy_approval(session, log.id)
    assert recovered.status == "pending"
    assert recovered.detail == "recovered; awaiting human approval"

    opened: list[int] = []

    class FakeEngine:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def open_trade(self, *args, **kwargs) -> int:
            opened.append(1)
            return 99

    monkeypatch.setattr(copy, "get_candidate", lambda *args, **kwargs: {"mint": "M"})
    monkeypatch.setattr("trading.ledger.TradeEngine", FakeEngine)
    result = copy.approve_copy(session, log.id, now=NOW + timedelta(seconds=60))
    assert result.status == "executed"
    assert len(opened) == 1


@pytest.mark.parametrize("detail", ["submission_started", "approval_in_progress"])
def test_recover_ambiguous_claim_requires_evidence(session, queue, enabled, detail) -> None:
    seed_market_asset(session)
    pending = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert pending is not None
    log = _rows(session, models.CopyLog)[0]
    log.status = "approving"
    log.detail = detail
    session.commit()

    with pytest.raises(TradeRefused, match="copy_approval_uncertain"):
        copy.recover_copy_approval(session, log.id)
    assert session.get(models.CopyLog, log.id).status == "approving"

    recovered = copy.recover_copy_approval(session, log.id, confirm_no_order=True)
    assert recovered.status == "pending"


def test_recover_submission_claim_with_position(session, queue, enabled) -> None:
    seed_market_asset(session)
    pending = copy.maybe_queue_copy(session, "W", "M", Decimal("1"), now=NOW)
    assert pending is not None
    log = _rows(session, models.CopyLog)[0]
    opened_at = NOW + timedelta(seconds=60)
    position = models.LedgerOpenPosition(
        mode="live",
        mint="M",
        chain="solana",
        source_signal="copy:1",
        entry_quote_price=1,
        entry_effective_price=1,
        qty=1,
        size_sol=1,
        entry_fees_sol=0,
        entry_slip_pct=0,
        is_new_token=False,
        stop_pct=5,
        target_pct=10,
        time_stop_minutes=None,
        stop_price=0.95,
        target_price=1.1,
        time_stop_at=None,
        opened_at=opened_at,
        price_source="test",
        price_source_ts=opened_at,
        candidate_evidence_ts=opened_at,
        why_entered="copy:W",
        rules_sha256="test",
        created_at=opened_at,
    )
    session.add(position)
    session.flush()
    log.status = "approving"
    log.detail = "submission_started"
    session.commit()

    recovered = copy.recover_copy_approval(session, log.id, position_id=position.id)
    assert recovered.status == "executed"
    assert recovered.detail == f"position_id={position.id}"
    assert copy.approve_copy(session, log.id).status == "executed"


def test_open_refusal_releases_claim(session, queue, enabled, monkeypatch) -> None:
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
            raise TradeRefused("no_live_price")

    monkeypatch.setattr(copy, "get_candidate", lambda *args, **kwargs: {"mint": "M"})
    monkeypatch.setattr("trading.ledger.TradeEngine", FakeEngine)

    refused = copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=60))
    assert refused.status == "refused"
    assert refused.detail == "no_live_price"
    original = session.get(models.CopyLog, pending.id)
    assert original is not None and original.status == "pending"

    second = copy.approve_copy(session, pending.id, now=NOW + timedelta(seconds=120))
    assert second.status == "refused"
    assert second.detail == "no_live_price"
    assert len(opened) == 2
    assert _rows(session, models.LedgerOpenPosition) == []
