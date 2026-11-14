"""Gated copy-trading (Phase F6).

Nothing here auto-approves or opens a trade. A copy is logged as ``pending``
and only a human calling the approve route can turn it into a live trade.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
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

_APPROVAL_CLAIMED = "approval_claimed"
_SUBMISSION_STARTED = "submission_started"
# Refusals that can only follow a venue submission. The venue may have accepted
# the order, so these must never release the claim for an automatic retry.
_POST_SUBMIT_REFUSAL_REASONS = frozenset({"fill_unconfirmed"})


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
    if not size_sol.is_finite() or size_sol <= 0:
        return None
    size = min(size_sol, settings.copy_max_size_sol)
    if size <= 0:
        return None
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


def _update_copy_state(
    session: Session,
    log_id: int,
    *,
    status: str,
    detail: str | None,
    require: str | None = None,
) -> bool:
    """Conditionally update one copy log, returning whether it matched."""
    statement = (
        update(models.CopyLog)
        .where(models.CopyLog.id == log_id)
        .values(status=status, detail=detail)
        .execution_options(synchronize_session=False)
    )
    if require is not None:
        statement = statement.where(models.CopyLog.status == require)
    result = session.execute(statement)
    return cast(CursorResult[Any], result).rowcount == 1


def _claim_pending_copy(session: Session, log_id: int) -> bool:
    """Atomically move one pending log to ``approving``.

    The conditional UPDATE is evaluated by the database, so two processes
    cannot both observe ``pending`` and then both proceed.
    """
    return _update_copy_state(
        session,
        log_id,
        status="approving",
        detail=_APPROVAL_CLAIMED,
        require="pending",
    )


def recover_copy_approval(
    session: Session,
    log_id: int,
    *,
    confirm_no_order: bool = False,
    position_id: int | None = None,
) -> models.CopyLog:
    """Recover a log left in ``approving`` by a crash.

    Only a freshly claimed row is safe to release automatically. Legacy or
    submitted claims may already have reached the venue, so they require either
    the opened position's id or an explicit operator confirmation that no order
    was sent.
    """
    pending = session.get(models.CopyLog, log_id)
    if pending is None:
        raise TradeRefused("no_pending_copy")
    session.refresh(pending)
    if pending.status in ("pending", "executed"):
        return pending
    if pending.status == "failed":
        raise TradeRefused("copy_approval_failed")
    if pending.status != "approving":
        raise TradeRefused("no_pending_copy")

    def _transition(status: str, detail: str, *, require: str = "approving") -> bool:
        def _persist(write_session: Session) -> bool:
            return _update_copy_state(
                write_session, pending.id, status=status, detail=detail, require=require
            )

        changed = queued_write(_persist)
        if changed:
            pending.status = status
            pending.detail = detail
        return changed

    if pending.detail != _APPROVAL_CLAIMED:
        if position_id is not None:
            position = session.get(models.LedgerOpenPosition, position_id)
            if (
                position is None
                or position.mint != pending.mint
                or position.mode != "live"
                or ensure_utc(position.opened_at) < ensure_utc(pending.ts)
            ):
                raise TradeRefused("position_mismatch")
            if not _transition("executed", f"position_id={position.id}"):
                raise TradeRefused("copy_approval_in_progress")
            return pending
        if not confirm_no_order:
            raise TradeRefused("copy_approval_uncertain")

    if not _transition("pending", "recovered; awaiting human approval"):
        raise TradeRefused("copy_approval_in_progress")
    return pending


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
    # Re-read after another request may have transitioned this row.
    session.refresh(pending)
    if pending.status == "executed":
        return pending
    if pending.status == "approving":
        raise TradeRefused("copy_approval_in_progress")
    if pending.status == "failed":
        raise TradeRefused("copy_approval_failed")
    if pending.status != "pending":
        raise TradeRefused("no_pending_copy")

    def _refuse(detail: str) -> models.CopyLog:
        return _log(
            session,
            wallet=pending.wallet,
            mint=pending.mint,
            size_sol=pending.size_sol,
            status="refused",
            detail=detail,
            now=now,
            use_write_queue=True,
        )

    def _set_state(status: str, detail: str | None, *, require: str | None = None) -> bool:
        def _persist(write_session: Session) -> bool:
            return _update_copy_state(
                write_session,
                pending.id,
                status=status,
                detail=detail,
                require=require,
            )

        changed = queued_write(_persist)
        if changed:
            pending.status = status
            pending.detail = detail
        return changed

    try:
        require_open(session)
    except LiveDisabled as exc:
        return _refuse(exc.reason)
    ready_at = ensure_utc(pending.ts) + timedelta(seconds=settings.copy_delay_seconds)
    if now < ready_at:
        return _refuse("copy_delay_not_elapsed")
    price_source = MarketSnapshotPriceSource(session)
    candidate = get_candidate(session, pending.mint, now=now, price_source=price_source)
    if candidate is None:
        return _refuse("candidate_not_found")
    original_detail = pending.detail

    def _claim(write_session: Session) -> bool:
        return _claim_pending_copy(write_session, pending.id)

    if not queued_write(_claim):
        session.expire_all()
        current = session.get(models.CopyLog, pending.id)
        if current is not None and current.status == "executed":
            return current
        raise TradeRefused("copy_approval_in_progress")
    pending.status = "approving"
    pending.detail = _APPROVAL_CLAIMED
    if not _set_state("approving", _SUBMISSION_STARTED, require="approving"):
        raise TradeRefused("copy_approval_in_progress")

    from trading.ledger import TradeEngine  # noqa: PLC0415 - avoids import cycle.

    try:
        position_id = TradeEngine(price_source, now=lambda: now).open_trade(
            session,
            candidate=candidate,
            mode="live",
            why_entered=f"copy:{pending.wallet}",
        )
    except TradeRefused as exc:
        if exc.reason in _POST_SUBMIT_REFUSAL_REASONS:
            # The venue may have accepted the order even though the fill was
            # not confirmed. Keep the claim so no retry submits a second live
            # order; reconcile through the recovery endpoint instead.
            raise
        # A known pre-submission refusal is safe to retry: release the claim
        # so a later approval can re-run the checks.
        _set_state("pending", original_detail, require="approving")
        return _refuse(exc.reason)
    except Exception:
        # The venue outcome is uncertain. Leave the claim in ``approving`` so
        # no retry can submit a second live order; reconcile manually.
        raise
    detail = f"position_id={position_id}"
    _set_state("executed", detail, require="approving")
    return pending
