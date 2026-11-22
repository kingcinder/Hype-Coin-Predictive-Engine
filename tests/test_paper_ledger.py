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
    def __init__(
        self,
        price: Decimal | None = Decimal("1"),
        ts: datetime = NOW,
        price_native: Decimal | None = None,
    ):
        self._price = price
        self._ts = ts
        self._price_native = price_native

    def price(self, mint: str, chain: str) -> PriceQuote | None:
        if self._price is None:
            return None
        return PriceQuote(
            price=self._price, ts=self._ts, source="fake", price_native=self._price_native
        )


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
            stop_pct=Decimal("0.1"),
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


def test_suggest_exits_time_stop_after_db_roundtrip(session, queue, tmp_path, monkeypatch) -> None:
    """time_stop_at round-trips through SQLite as naive; comparing it with an
    aware now must not raise (production 500 on 2026-11-20, exposed once a
    paper trade could actually open)."""
    asset = seed_market_asset(session)
    session.commit()
    path = tmp_path / "rules-time-stop.json"
    path.write_text(
        json.dumps(
            {
                "buy_trigger": "ignition_detected",
                "stop_pct": 20,
                "target_pct": 50,
                "time_stop_minutes": 60,
            }
        )
    )
    monkeypatch.setattr(get_settings(), "trading_rules_path", str(path))
    engine = _engine()
    engine.open_trade(session, candidate=_candidate(asset), mode="paper", why_entered="x")
    session.expire_all()
    # Before the time stop: no suggestion, and no TypeError from the
    # naive/aware comparison.
    assert engine.suggest_exits(session) == []
    # After the time stop: a time_stop suggestion.
    later = _engine()
    later._now = lambda: NOW + timedelta(hours=2)
    suggestions = later.suggest_exits(session)
    assert len(suggestions) == 1
    assert suggestions[0]["reason"] == "time_stop"


def test_no_update_or_delete_helpers() -> None:
    import trading.ledger as ledger

    assert [name for name in dir(ledger) if name.startswith(("update_", "delete_"))] == []


def test_open_with_native_price_stores_real_token_qty(session, queue, rules_file) -> None:
    # SOL $100: $1/token == 0.01 SOL/token. New-token 50% slip (age 5 min).
    source = FakePriceSource(price=Decimal("1"), price_native=Decimal("0.01"))
    asset = seed_market_asset(session)
    session.commit()
    engine = TradeEngine(source, now=lambda: NOW)
    position_id = engine.open_trade(
        session, candidate=_candidate(asset), mode="paper", why_entered="native entry"
    )
    session.expire_all()
    position = session.get(models.LedgerOpenPosition, position_id)
    assert position is not None
    # net 0.494 SOL / (0.01 * 1.5) SOL per token.
    assert position.qty == Decimal("32.933333333333")
    # SQLite stores NUMERIC as float: 0.015 round-trips as
    # 0.014999999999999999 at the column's 18-decimal scale. The value is
    # only ever used as a ratio (entry USD / entry native), where the
    # 1e-18 noise is irrelevant -- compare with tolerance, not exactly.
    assert abs(position.entry_price_native - Decimal("0.015")) < Decimal("1e-15")
    assert position.entry_effective_price == Decimal("1.5")

    trade_id = engine.close_trade(session, position_id, why_exited="flat exit", lesson="flat")
    session.expire_all()
    trade = session.get(models.LedgerTrade, trade_id)
    buy = simulate_buy(
        size_sol=Decimal("0.5"),
        quote_price=Decimal("1"),
        slip_pct=Decimal("50"),
        fee_pct=Decimal("1"),
        priority_sol=Decimal("0.001"),
        price_native=Decimal("0.01"),
    )
    proceeds, _ = simulate_sell(
        qty=buy.qty,
        quote_price=Decimal("1"),
        slip_pct=Decimal("50"),
        fee_pct=Decimal("1"),
        priority_sol=Decimal("0.001"),
        price_native=Decimal("0.01"),
    )
    assert trade.pnl_sol == proceeds - Decimal("0.5")


def test_legacy_position_closes_on_usd_ratio_not_native(session, queue, rules_file) -> None:
    # BANDANA CAT regression: opened before the fix (no native price), so
    # its qty is the scaled count. Closing at the live price must use the
    # USD ratio -- never qty x native price, which would book phantom P&L
    # on mixed units.
    source = FakePriceSource(price=Decimal("0.000000001326"))
    asset = seed_market_asset(session)
    session.commit()
    engine = TradeEngine(source, now=lambda: NOW)
    position_id = engine.open_trade(
        session,
        candidate=_candidate(asset, age_minutes=73),
        mode="paper",
        why_entered="legacy entry",
    )
    session.expire_all()
    position = session.get(models.LedgerOpenPosition, position_id)
    assert position is not None
    assert position.entry_price_native is None
    legacy_qty = position.qty

    source._price = Decimal("0.0001916")
    source._price_native = Decimal("0.000002780")
    trade_id = engine.close_trade(session, position_id, why_exited="target", lesson="live price")
    session.expire_all()
    trade = session.get(models.LedgerTrade, trade_id)
    proceeds, _ = simulate_sell(
        qty=legacy_qty,
        quote_price=Decimal("0.0001916"),
        slip_pct=Decimal("30"),
        fee_pct=Decimal("1"),
        priority_sol=Decimal("0.001"),
    )
    assert trade.pnl_sol == proceeds - Decimal("0.5")
    assert trade.pnl_sol > Decimal("1000")  # phantom entry price, honestly huge


def test_close_without_native_exit_price_uses_entry_implied_sol_usd(
    session, queue, rules_file
) -> None:
    # Open with native pricing (SOL $100), close on a USD-only quote: the
    # exit native price is the USD price converted at the entry-implied
    # SOL/USD rate, so proceeds stay true SOL.
    source = FakePriceSource(price=Decimal("1"), price_native=Decimal("0.01"))
    asset = seed_market_asset(session)
    session.commit()
    engine = TradeEngine(source, now=lambda: NOW)
    position_id = engine.open_trade(
        session, candidate=_candidate(asset), mode="paper", why_entered="native entry"
    )
    session.expire_all()
    position = session.get(models.LedgerOpenPosition, position_id)
    assert position is not None
    qty = position.qty

    source._price = Decimal("2")
    source._price_native = None
    trade_id = engine.close_trade(session, position_id, why_exited="target", lesson="usd-only exit")
    session.expire_all()
    trade = session.get(models.LedgerTrade, trade_id)
    # Entry-implied SOL/USD = 1.5 / 0.015 = 100, so exit native = 2/100.
    proceeds, _ = simulate_sell(
        qty=qty,
        quote_price=Decimal("2"),
        slip_pct=Decimal("50"),
        fee_pct=Decimal("1"),
        priority_sol=Decimal("0.001"),
        price_native=Decimal("0.02"),
    )
    assert trade.pnl_sol == proceeds - Decimal("0.5")
