from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from storage import models
from trading.errors import LiveDisabled

ARM_PHRASE = "ENABLE LIVE TRADING"


def _latest(session: Session, kind: str):
    return session.scalar(
        select(func.max(models.LiveGateState.ts)).where(models.LiveGateState.kind == kind)
    )


def is_open(session: Session) -> bool:
    s = get_settings()
    if s.live_trading_enabled != "1":
        return False
    armed = _latest(session, "armed")
    if armed is None:
        return False
    killed = _latest(session, "killed")
    return killed is None or killed < armed


def require_open(session: Session) -> None:
    s = get_settings()
    if s.live_trading_enabled != "1":
        raise LiveDisabled("env_flag_off")
    if not is_open(session):
        raise LiveDisabled("not_armed_or_killed")
    if s.live_max_bankroll_sol is None:
        raise LiveDisabled("live_max_bankroll_unset")
    if s.live_daily_loss_limit_sol is None:
        raise LiveDisabled("live_daily_loss_limit_unset")


def effective_risk_pct() -> Decimal:
    s = get_settings()
    return min(s.live_max_risk_pct, Decimal("1"))  # hard cap 1%


def daily_loss_sol(session: Session, now: datetime | None = None) -> Decimal:
    now = now or datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = session.scalar(
        select(func.coalesce(func.sum(models.LedgerTrade.pnl_sol), 0)).where(
            models.LedgerTrade.mode == "live",
            models.LedgerTrade.closed_at >= start,
        )
    )
    return -min(Decimal(str(total)), Decimal("0"))  # positive number = loss today
