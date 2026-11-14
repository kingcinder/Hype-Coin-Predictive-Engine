"""Gated copy-trading (Phase F6).

Nothing here auto-approves or opens a trade. A copy is logged as ``pending``
and only a human calling the approve route can turn it into a live trade.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from common.config import get_settings
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import queued_write
from trading.candidates import get_candidate
from trading.errors import LiveDisabled, TradeRefused
from trading.live_gate import effective_risk_pct, require_open
from trading.price_sources import MarketSnapshotPriceSource
from trading.wallets import load_wallets


def _log(
    session: Session,
    *,
    wallet: str,
    mint: str,
    size_sol: Decimal,
    status: str,
    detail: str | None,
    now: datetime,
    use_write_queue: bool,
) -> models.CopyLog:
    row = models.CopyLog(
        ts=now,
        wallet=wallet,
        mint=mint,
        size_sol=size_sol,
        status=status,
        detail=detail,
    )
    if use_write_queue:

        def _persist(write_session: Session) -> None:
            write_session.add(row)

        queued_write(_persist)
    else:
        # The ingestion path already owns SQLite's write transaction; adding
        # here commits with that stage instead of waiting on the writer thread.
        session.add(row)
        session.flush()
    return row


def maybe_queue_copy(
    session: Session,
    wallet: str,
    mint: str,
    size_sol: Decimal,
    *,
    now: datetime | None = None,
    use_write_queue: bool = True,
) -> models.CopyLog | None:
    """Log a copy signal; never execute it."""
    settings = get_settings()
    if not settings.copy_trading_enabled:
        return None
    addresses = {entry["address"] for entry in load_wallets()}
    if wallet not in addresses:
        return None
    now = ensure_utc(now or utc_now())
    size = min(Decimal(str(size_sol)), settings.copy_max_size_sol)
    risk_pct = min(settings.copy_max_risk_pct, effective_risk_pct())
    # The gate itself is checked here so a closed gate produces the paper-only log.
    try:
        require_open(session)
    except LiveDisabled as exc:
        return _log(
            session,
            wallet=wallet,
            mint=mint,
            size_sol=size,
            status="paper_only",
            detail=f"{exc.reason}; risk_pct={risk_pct}",
            now=now,
            use_write_queue=use_write_queue,
        )
    return _log(
        session,
        wallet=wallet,
        mint=mint,
        size_sol=size,
        status="pending",
        detail=f"awaiting human approval; risk_pct={risk_pct}",
        now=now,
        use_write_queue=use_write_queue,
    )


def approve_copy(
    session: Session,
    log_id: int,
    *,
    now: datetime | None = None,
) -> models.CopyLog:
    """Execute a pending copy after a human approval, or log a refusal."""
    settings = get_settings()
    now = ensure_utc(now or utc_now())
    pending = session.get(models.CopyLog, log_id)
    if pending is None:
        raise TradeRefused("no_pending_copy")
    if pending.status == "executed":
        return pending
    if pending.status == "approving":
        raise TradeRefused("copy_approval_in_progress")
    if pending.status != "pending":
        raise TradeRefused("no_pending_copy")
    try:
        require_open(session)
    except LiveDisabled as exc:
        return _log(
            session,
            wallet=pending.wallet,
            mint=pending.mint,
            size_sol=pending.size_sol,
            status="refused",
            detail=exc.reason,
            now=now,
            use_write_queue=True,
        )
    ready_at = ensure_utc(pending.ts) + timedelta(seconds=settings.copy_delay_seconds)
    if now < ready_at:
        return _log(
            session,
            wallet=pending.wallet,
            mint=pending.mint,
            size_sol=pending.size_sol,
            status="refused",
            detail="copy_delay_not_elapsed",
            now=now,
            use_write_queue=True,
        )
    price_source = MarketSnapshotPriceSource(session)
    candidate = get_candidate(session, pending.mint, now=now, price_source=price_source)
    if candidate is None:
        return _log(
            session,
            wallet=pending.wallet,
            mint=pending.mint,
            size_sol=pending.size_sol,
            status="refused",
            detail="candidate_not_found",
            now=now,
            use_write_queue=True,
        )
    original_detail = pending.detail

    def _claim(write_session: Session) -> bool:
        row = write_session.get(models.CopyLog, pending.id)
        if row is None or row.status != "pending":
            return False
        row.status = "approving"
        row.detail = "approval_in_progress"
        return True

    if not queued_write(_claim):
        session.expire_all()
        current = session.get(models.CopyLog, pending.id)
        if current is not None and current.status == "executed":
            return current
        raise TradeRefused("copy_approval_in_progress")
    pending.status = "approving"
    pending.detail = "approval_in_progress"

    def _release_claim() -> None:
        def _persist(write_session: Session) -> None:
            row = write_session.get(models.CopyLog, pending.id)
            if row is not None:
                row.status = "pending"
                row.detail = original_detail

        queued_write(_persist)
        pending.status = "pending"
        pending.detail = original_detail

    from trading.ledger import TradeEngine  # noqa: PLC0415 - avoids import cycle.

    try:
        position_id = TradeEngine(price_source, now=lambda: now).open_trade(
            session,
            candidate=candidate,
            mode="live",
            why_entered=f"copy:{pending.wallet}",
        )
    except TradeRefused as exc:
        _release_claim()
        return _log(
            session,
            wallet=pending.wallet,
            mint=pending.mint,
            size_sol=pending.size_sol,
            status="refused",
            detail=exc.reason,
            now=now,
            use_write_queue=True,
        )
    except Exception:
        _release_claim()
        raise
    detail = f"position_id={position_id}"

    def _mark_executed(write_session: Session) -> None:
        row = write_session.get(models.CopyLog, pending.id)
        if row is not None:
            row.status = "executed"
            row.detail = detail

    queued_write(_mark_executed)
    pending.status = "executed"
    pending.detail = detail
    return pending
