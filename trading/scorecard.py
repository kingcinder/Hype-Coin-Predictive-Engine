from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from common.config import get_settings
from storage import models
from storage.repository import queued_write
from trading.costs import (
    HUNDRED,
    q_sol,
    simulate_buy,
    simulate_sell,
    slippage_pct,
)
from validation.metrics import bootstrap_ci

ZERO = Decimal("0")


def _pnl(trade) -> Decimal:
    value = trade.pnl_sol
    return Decimal(str(value)) if value is not None else ZERO


MIN_TRADES_FOR_EDGE = 30
SNAPSHOT_EVERY = 10
SNAPSHOT_MAX_AGE = timedelta(days=7)


def _baseline_pnl(size_sol: Decimal, price: Decimal) -> Decimal:
    settings = get_settings()
    slip = slippage_pct(False, settings.slippage_pct_normal, settings.slippage_pct_new_token)
    buy = simulate_buy(
        size_sol=size_sol,
        quote_price=price,
        slip_pct=slip,
        fee_pct=settings.gmgn_fee_pct_per_tx,
        priority_sol=settings.priority_fee_sol,
    )
    proceeds, _ = simulate_sell(
        qty=buy.qty,
        quote_price=price,
        slip_pct=slip,
        fee_pct=settings.gmgn_fee_pct_per_tx,
        priority_sol=settings.priority_fee_sol,
    )
    return q_sol(proceeds - size_sol)


def build_scorecard(session, mode: str, bankroll_base: Decimal) -> dict:
    trades = session.scalars(
        select(models.LedgerTrade)
        .where(
            models.LedgerTrade.mode == mode,
            models.LedgerTrade.closed_at.is_not(None),
        )
        .order_by(models.LedgerTrade.closed_at)
    ).all()

    n = len(trades)
    wins = sum(1 for t in trades if _pnl(t) > 0)
    losses = n - wins
    win_pnls: list[Decimal] = [_pnl(t) for t in trades if _pnl(t) > 0]
    loss_pnls: list[Decimal] = [_pnl(t) for t in trades if _pnl(t) <= 0]

    win_rate = Decimal(str(wins / n)).quantize(Decimal("0.001")) if n else None
    avg_win = (sum(win_pnls, ZERO) / len(win_pnls)).quantize(Decimal("0.001")) if win_pnls else None
    avg_loss = (
        (sum(loss_pnls, ZERO) / len(loss_pnls)).quantize(Decimal("0.001")) if loss_pnls else None
    )
    net_pnl = sum((_pnl(t) for t in trades), ZERO).quantize(Decimal("0.001"))
    bankroll_now = bankroll_base + net_pnl

    peak = bankroll_base
    equity = bankroll_base
    max_decline = ZERO
    for trade in trades:
        equity += _pnl(trade)
        peak = max(peak, equity)
        max_decline = max(max_decline, peak - equity)
    max_drawdown = (max_decline / peak * HUNDRED).quantize(Decimal("0.01")) if peak else ZERO

    baseline_mean = ZERO
    edge_check: dict = {
        "verdict": "insufficient_data",
        "n": n,
        "mean_pnl_sol": (net_pnl / n) if n else None,
        "ci_low": None,
        "ci_high": None,
        "baseline_mean_sol": None,
    }
    if n:
        baselines = [_baseline_pnl(t.size_sol, t.entry_price) for t in trades]
        differences = [(_pnl(t)) - b for t, b in zip(trades, baselines, strict=True)]
        baseline_mean = (sum(baselines, ZERO) / n).quantize(Decimal("0.001"))
        mean_diff = sum(differences, ZERO) / n
        edge_check = {
            "verdict": "insufficient_data",
            "n": n,
            "mean_pnl_sol": mean_diff.quantize(Decimal("0.001")),
            "baseline_mean_sol": baseline_mean,
            "ci_low": None,
            "ci_high": None,
        }
        if n >= MIN_TRADES_FOR_EDGE:
            # Each trade is its own group: no trade is resampled as many rows.
            ci_low, ci_high = bootstrap_ci(
                [[float(d)] for d in differences], lambda values: float(sum(values) / len(values))
            )
            if ci_low > 0:
                verdict = "beats_naive_baseline"
            elif ci_high < 0:
                verdict = "worse_than_naive_baseline"
            else:
                verdict = "inconclusive"
            edge_check.update(
                {
                    "verdict": verdict,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                }
            )

    return {
        "mode": mode,
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "avg_win_sol": avg_win,
        "avg_loss_sol": avg_loss,
        "net_pnl_sol": net_pnl,
        "bankroll_now": bankroll_now,
        "max_drawdown_pct": max_drawdown,
        "edge_check": edge_check,
        "generated_at": datetime.now(UTC),
    }


def maybe_snapshot_scorecard(session, mode: str, bankroll_base: Decimal) -> None:
    count = session.scalar(
        select(func.count(models.LedgerTrade.id)).where(
            models.LedgerTrade.mode == mode,
            models.LedgerTrade.closed_at.is_not(None),
        )
    )
    if not count or count % SNAPSHOT_EVERY != 0:
        return
    last = session.scalar(
        select(models.LedgerScorecard)
        .where(models.LedgerScorecard.mode == mode)
        .order_by(models.LedgerScorecard.created_at.desc())
        .limit(1)
    )
    now = datetime.now(UTC)
    if last is not None:
        last_at = last.created_at
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=UTC)
        payload_count = (last.payload or {}).get("trades")
        if payload_count == count or now - last_at < SNAPSHOT_MAX_AGE:
            return

    card = build_scorecard(session, mode, bankroll_base)
    payload = {
        key: (str(value) if isinstance(value, Decimal) else value)
        for key, value in card.items()
        if key != "generated_at"
    }
    payload["generated_at"] = card["generated_at"].isoformat()
    payload["trades"] = count

    def _persist(write_session) -> None:
        write_session.add(models.LedgerScorecard(mode=mode, payload=payload, created_at=now))

    queued_write(_persist)
