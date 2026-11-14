from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

SOL = Decimal("0.000000001")
QTY = Decimal("0.000000000001")
HUNDRED = Decimal("100")


def q_sol(x: Decimal) -> Decimal:
    return x.quantize(SOL, rounding=ROUND_HALF_EVEN)


def q_qty(x: Decimal) -> Decimal:
    return x.quantize(QTY, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class Fill:
    effective_price: Decimal
    qty: Decimal
    fee_sol: Decimal
    priority_sol: Decimal
    slippage_pct: Decimal


def slippage_pct(is_new_token: bool, normal: Decimal, new: Decimal) -> Decimal:
    return new if is_new_token else normal


def simulate_buy(
    *,
    size_sol: Decimal,
    quote_price: Decimal,
    slip_pct: Decimal,
    fee_pct: Decimal,
    priority_sol: Decimal,
) -> Fill:
    """Buy: pay fee+priority out of size, buy at a WORSE price (price*(1+slip))."""
    fee = q_sol(size_sol * fee_pct / HUNDRED)
    net = size_sol - fee - priority_sol
    eff = quote_price * (Decimal(1) + slip_pct / HUNDRED)
    return Fill(eff, q_qty(net / eff), fee, priority_sol, slip_pct)


def simulate_sell(
    *,
    qty: Decimal,
    quote_price: Decimal,
    slip_pct: Decimal,
    fee_pct: Decimal,
    priority_sol: Decimal,
) -> tuple[Decimal, Fill]:
    """Sell: receive at a WORSE price (price*(1-slip)); fee+priority come off proceeds.
    Returns (net_proceeds_sol, Fill).
    """
    eff = quote_price * (Decimal(1) - slip_pct / HUNDRED)
    gross = q_sol(qty * eff)
    fee = q_sol(gross * fee_pct / HUNDRED)
    proceeds = gross - fee - priority_sol
    return proceeds, Fill(eff, qty, fee, priority_sol, slip_pct)


def position_size_sol(
    *,
    bankroll: Decimal,
    risk_pct: Decimal,
    stop_pct: Decimal,
) -> Decimal:
    """size = bankroll * risk% / stop%, capped at bankroll. stop_pct must be in (0,100)."""
    if stop_pct <= 0 or stop_pct >= HUNDRED:
        raise ValueError("stop_pct must be >0 and <100")
    size = bankroll * (risk_pct / HUNDRED) / (stop_pct / HUNDRED)
    return q_sol(min(size, bankroll))
