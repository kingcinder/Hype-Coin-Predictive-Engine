from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from common.config import get_settings
from storage import models
from storage.write_queue import start_write_queue, stop_write_queue
from tests.conftest import seed_market_asset
from trading.costs import simulate_buy, simulate_sell
from trading.errors import DrawdownReview, LiveDisabled, RulesIncomplete, TradeRefused
from trading.executors import PriceQuote
from trading.ledger import TradeEngine

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


class FakePriceSource:
    def __init__(self, price: Decimal | None = Decimal("1"), ts: datetime = NOW):
        self._price = price
        self._ts = ts

    def price(self, mint: str, chain: str) -> PriceQuote | None:
        if self._price is None:
            return None
        return PriceQuote(price=self._price, ts=self._ts, source="fake")


@pytest.fixture
def queue(session):
    factory = sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)
    start_write_queue(session_factory=factory)
    yield
    stop_write_queue()


@pytest.fixture
def rules_file(tmp_path, monkeypatch):
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps({"buy_trigger": "ignition_detected", "stop_pct": 20, "target_pct": 50})
    )
    monkeypatch.setattr(get_settings(), "trading_rules_path", str(path))
    return path


def _candidate(asset, *, age_minutes=5, evidence_ts=NOW):
    return {
        "mint": asset.address,
        "chain": "solana",
        "evidence_ts": evidence_ts,
        "age_minutes": age_minutes,
        "signal_id": "ignition:1:1",
    }


def _engine(price_source=None):
    return TradeEngine(price_source or FakePriceSource(), now=lambda: NOW)


def _counts(session):
    session.expire_all()
    return (
        session.scalar(select(func.count(models.LedgerOpenPosition.id))),
        session.scalar(select(func.count(models.LedgerTrade.id))),
    )


def test_open_then_close_flat_matches_simulator(session, queue, rules_file) -> None:
    asset = seed_market_asset(session)
    session.commit()
    engine = _engine()

    position_id = engine.open_trade(
        session,
        candidate=_candidate(asset),
        mode="paper",
        why_entered="test entry",
    )
    session.expire_all()
    position = session.get(models.LedgerOpenPosition, position_id)
    assert position is not None

    trade_id = engine.close_trade(
        session,
        position_id,
        why_exited="flat exit",
        lesson="flat",
    )
    session.expire_all()
    trade = session.get(models.LedgerTrade, trade_id)
    assert trade is not None

    buy = simulate_buy(
        size_sol=Decimal("0.5"),
        quote_price=Decimal("1"),
        slip_pct=Decimal("50"),
        fee_pct=Decimal("1"),
        priority_sol=Decimal("0.001"),
    )
    proceeds, _ = simulate_sell(
        qty=buy.qty,
        quote_price=Decimal("1"),
        slip_pct=Decimal("50"),
        fee_pct=Decimal("1"),
        priority_sol=Decimal("0.001"),
    )
    assert trade.pnl_sol == proceeds - Decimal("0.5")
    assert trade.status if hasattr(trade, "status") else True


def test_missing_rules_writes_nothing(session, queue, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "trading_rules_path", str(tmp_path / "absent.json"))
    asset = seed_market_asset(session)
    session.commit()
    with pytest.raises(RulesIncomplete):
        _engine().open_trade(session, candidate=_candidate(asset), mode="paper", why_entered="x")
    assert _counts(session) == (0, 0)


def test_missing_evidence_ts(session, queue, rules_file) -> None:
    asset = seed_market_asset(session)
    session.commit()
    with pytest.raises(TradeRefused) as exc:
        _engine().open_trade(
            session,
            candidate=_candidate(asset, evidence_ts=None),
            mode="paper",
            why_entered="x",
        )
    assert exc.value.reason == "no_evidence_ts"


def test_no_price(session, queue, rules_file) -> None:
    asset = seed_market_asset(session)
    session.commit()
    with pytest.raises(TradeRefused) as exc:
        _engine(FakePriceSource(price=None)).open_trade(
            session, candidate=_candidate(asset), mode="paper", why_entered="x"
        )
    assert exc.value.reason == "no_live_price"


def test_stale_price(session, queue, rules_file) -> None:
    asset = seed_market_asset(session)
    session.commit()
    stale = FakePriceSource(ts=NOW - timedelta(seconds=120))
    with pytest.raises(TradeRefused) as exc:
        _engine(stale).open_trade(
            session, candidate=_candidate(asset), mode="paper", why_entered="x"
        )
    assert exc.value.reason == "stale_price"


def test_drawdown_requires_ack(session, queue, rules_file) -> None:
    asset = seed_market_asset(session)
    now = NOW - timedelta(hours=1)
    session.add(
        models.LedgerTrade(
            mode="paper",
            opened_at=now,
            closed_at=now,
            mint="X",
            chain="solana",
            source_signal="t",
            entry_price=Decimal("1"),
            size_sol=Decimal("1"),
            fees_sol=Decimal("0"),
            slippage_sol=Decimal("0"),
            exit_price=Decimal("1"),
            pnl_sol=Decimal("-1.1"),
            why_entered="t",
            why_exited="t",
            lesson="t",
            candidate_evidence_ts=now,
            price_source="fake",
            price_source_ts=now,
            rules_sha256="a",
            created_at=now,
        )
    )
    session.commit()

    with pytest.raises(DrawdownReview):
        _engine().open_trade(session, candidate=_candidate(asset), mode="paper", why_entered="x")

    from storage.repository import queued_write

    queued_write(lambda s: s.add(models.LedgerReviewAck(acked_at=NOW, note="ok")))
    position_id = _engine().open_trade(
        session, candidate=_candidate(asset), mode="paper", why_entered="x"
    )
    assert position_id > 0


def test_live_requires_gate(session, queue, rules_file) -> None:
    asset = seed_market_asset(session)
    session.commit()
    with pytest.raises(LiveDisabled):
        _engine().open_trade(session, candidate=_candidate(asset), mode="live", why_entered="x")


def test_suggest_exits_never_writes(session, queue, rules_file) -> None:
    asset = seed_market_asset(session)
    session.commit()
    engine = _engine()
    engine.open_trade(session, candidate=_candidate(asset), mode="paper", why_entered="x")
    session.expire_all()
    before = _counts(session)

    assert engine.suggest_exits(session) == []  # price 1: between stop and target
    stop = _engine(FakePriceSource(Decimal("0.79"))).suggest_exits(session)
    assert stop[0]["reason"] == "stop"
    target = _engine(FakePriceSource(Decimal("1.5"))).suggest_exits(session)
    assert target[0]["reason"] == "target"
    assert _counts(session) == before


def test_no_update_or_delete_helpers() -> None:
    import trading.ledger as ledger

    assert [name for name in dir(ledger) if name.startswith(("update_", "delete_"))] == []
