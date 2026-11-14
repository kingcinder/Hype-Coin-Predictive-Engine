from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from storage import models
from trading.scorecard import build_scorecard


def _trade(session, pnl: Decimal) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    session.add(
        models.LedgerTrade(
            mode="paper",
            opened_at=now,
            closed_at=now,
            mint="SOLUSD",
            chain="solana",
            source_signal="test",
            entry_price=Decimal("1"),
            size_sol=Decimal("1"),
            fees_sol=Decimal("0"),
            slippage_sol=Decimal("0"),
            stop_pct=Decimal("0.1"),
            exit_price=Decimal("1"),
            pnl_sol=pnl,
            why_entered="test",
            why_exited="test",
            lesson="test",
            candidate_evidence_ts=now,
            price_source="fake",
            price_source_ts=now,
            rules_sha256="abc",
            created_at=now,
        )
    )
    session.flush()


def test_four_trades_exact(session) -> None:
    for pnl in ("0.2", "0.4", "-0.1", "-0.3"):
        _trade(session, Decimal(pnl))

    card = build_scorecard(session, "paper", Decimal("10"))

    assert card["trades"] == 4
    assert card["wins"] == 2
    assert card["losses"] == 2
    assert card["win_rate"] == Decimal("0.5")
    assert card["avg_win_sol"] == Decimal("0.3")
    assert card["avg_loss_sol"] == Decimal("-0.2")
    assert card["net_pnl_sol"] == Decimal("0.2")
    assert card["edge_check"]["verdict"] == "insufficient_data"
    assert card["edge_check"]["baseline_mean_sol"] is not None


def test_zero_trades(session) -> None:
    card = build_scorecard(session, "paper", Decimal("10"))
    assert card["trades"] == 0
    assert card["win_rate"] is None
    assert card["avg_win_sol"] is None
    assert card["avg_loss_sol"] is None
    assert card["net_pnl_sol"] == Decimal("0")
