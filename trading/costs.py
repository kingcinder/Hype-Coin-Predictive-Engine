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
    # Slippage-adjusted per-token price in the chain's native currency
    # (SOL on Solana), parallel to effective_price (USD/token). Set only
    # when the quote carried a native price; None otherwise.
    effective_price_native: Decimal | None = None


def slippage_pct(is_new_token: bool, normal: Decimal, new: Decimal) -> Decimal:
    return new if is_new_token else normal


def simulate_buy(
    *,
    size_sol: Decimal,
    quote_price: Decimal,
    slip_pct: Decimal,
    fee_pct: Decimal,
    priority_sol: Decimal,
    price_native: Decimal | None = None,
) -> Fill:
    """Buy: pay fee+priority out of size, buy at a WORSE price (price*(1+slip)).

    ``quote_price`` is USD/token; ``price_native`` (when given) is the same
    price in the chain's native currency (SOL/token on Solana). ``qty`` is a
    real token count only when ``price_native`` is given: qty =
    net_sol / effective_native_price. Without it, qty falls back to
    net_sol / effective_usd_price -- a scaled count, NOT tokens (it is
    SOL/USD too small: BANDANA CAT recorded 286,576,168 vs ~19.67e9 real
    tokens at SOL $68.64). Callers must pass the matching price to
    simulate_sell: native qty with a native price, scaled qty with USD.
    """
    fee = q_sol(size_sol * fee_pct / HUNDRED)
    net = size_sol - fee - priority_sol
    eff = quote_price * (Decimal(1) + slip_pct / HUNDRED)
    if price_native is not None and price_native > 0:
        eff_native = price_native * (Decimal(1) + slip_pct / HUNDRED)
        return Fill(eff, q_qty(net / eff_native), fee, priority_sol, slip_pct, eff_native)
    return Fill(eff, q_qty(net / eff), fee, priority_sol, slip_pct)


def simulate_sell(
    *,
    qty: Decimal,
    quote_price: Decimal,
    slip_pct: Decimal,
    fee_pct: Decimal,
    priority_sol: Decimal,
    price_native: Decimal | None = None,
) -> tuple[Decimal, Fill]:
    """Sell: receive at a WORSE price (price*(1-slip)); fee+priority come off proceeds.
    Returns (net_proceeds_sol, Fill).

    When ``price_native`` is given, ``qty`` must be a real token count and
    proceeds are true SOL (qty x native price). Otherwise ``quote_price``
    (USD/token) is used and ``qty`` must be the scaled count from the
    matching simulate_buy call -- the exit/entry ratio still yields SOL,
    but the qty itself is not a token count.
    """
    price = price_native if price_native is not None and price_native > 0 else quote_price
    eff = price * (Decimal(1) - slip_pct / HUNDRED)
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
