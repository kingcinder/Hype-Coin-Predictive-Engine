from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.auth import DbSession
from common.config import get_settings
from common.time import utc_now
from storage import models
from storage.repository import queued_write
from trading.candidates import build_candidates, get_candidate
from trading.errors import DrawdownReview, LiveDisabled, RulesIncomplete, TradeRefused
from trading.ledger import TradeEngine
from trading.price_sources import MarketSnapshotPriceSource
from trading.scorecard import build_scorecard
from validation.status import scores_validation_status

router = APIRouter()


class TradeBody(BaseModel):
    mint: str
    chain: str
    source_signal: str | None = None
    why_entered: str


class ExitBody(BaseModel):
    why_exited: str
    lesson: str


class AckBody(BaseModel):
    note: str | None = None


class RecoverCopyBody(BaseModel):
    confirm_no_order: bool = False
    position_id: int | None = Field(default=None, ge=1)


class CandidateTradeBody(BaseModel):
    why_entered: str
    mode: str = "paper"


class CopySignalBody(BaseModel):
    wallet: str = Field(min_length=1)
    mint: str = Field(min_length=1)
    size_sol: Decimal = Field(gt=0)


def _json_value(value):
    return str(value) if isinstance(value, Decimal) else value


def _raise_mapped(exc: Exception) -> None:
    if isinstance(exc, RulesIncomplete):
        raise HTTPException(409, {"error": "rules_incomplete", "missing": exc.missing}) from exc
    if isinstance(exc, DrawdownReview):
        raise HTTPException(409, {"error": "drawdown_review"}) from exc
    if isinstance(exc, LiveDisabled):
        raise HTTPException(403, {"error": "live_disabled", "detail": exc.detail}) from exc
    if isinstance(exc, TradeRefused):
        raise HTTPException(422, {"error": exc.reason}) from exc
    raise exc


@router.post("/paper/trades")
async def create_paper_trade(body: TradeBody, session: DbSession) -> dict:
    price_source = MarketSnapshotPriceSource(session)
    candidate = get_candidate(session, body.mint, now=utc_now(), price_source=price_source)
    if candidate is None:
        raise HTTPException(422, {"error": "candidate_not_found"})
    try:
        position_id = TradeEngine(price_source, now=utc_now).open_trade(
            session,
            candidate=candidate,
            mode="paper",
            why_entered=body.why_entered,
        )
    except (TradeRefused, RulesIncomplete) as exc:  # RulesIncomplete subclasses TradeRefused
        _raise_mapped(exc)
    return {"status": "opened", "position_id": position_id}


@router.post("/paper/trades/{position_id}/exit")
async def exit_paper_trade(position_id: int, body: ExitBody, session: DbSession) -> dict:
    try:
        trade_id = TradeEngine(MarketSnapshotPriceSource(session), now=utc_now).close_trade(
            session,
            position_id,
            why_exited=body.why_exited,
            lesson=body.lesson,
        )
    except TradeRefused as exc:
        _raise_mapped(exc)
    return {"status": "closed", "trade_id": trade_id}


@router.get("/paper/ledger")
async def paper_ledger(session: DbSession) -> dict:
    open_positions = session.scalars(select(models.LedgerOpenPosition)).all()
    closed = session.scalars(
        select(models.LedgerTrade).order_by(models.LedgerTrade.closed_at.desc()).limit(200)
    ).all()
    suggestions = TradeEngine(MarketSnapshotPriceSource(session), now=utc_now).suggest_exits(
        session
    )
    return {
        "closed": [
            {
                "id": row.id,
                "mint": row.mint,
                "chain": row.chain,
                "pnl_sol": str(row.pnl_sol),
                "why_exited": row.why_exited,
                "closed_at": row.closed_at.isoformat(),
            }
            for row in closed
        ],
        "open": [
            {
                "id": row.id,
                "mint": row.mint,
                "chain": row.chain,
                "size_sol": str(row.size_sol),
                "entry_effective_price": str(row.entry_effective_price),
                "stop_price": str(row.stop_price),
                "target_price": None if row.target_price is None else str(row.target_price),
                "opened_at": row.opened_at.isoformat(),
            }
            for row in open_positions
        ],
        "exit_suggestions": [
            {key: _json_value(value) for key, value in item.items()} for item in suggestions
        ],
    }


@router.get("/paper/scorecard")
async def paper_scorecard(session: DbSession, mode: str = "paper") -> dict:
    settings = get_settings()
    card = build_scorecard(session, mode, settings.paper_bankroll_sol)
    return {key: _json_value(value) for key, value in card.items() if key != "generated_at"} | {
        "generated_at": card["generated_at"].isoformat()
    }


@router.post("/paper/review/ack")
async def paper_review_ack(session: DbSession, body: AckBody | None = None) -> dict:
    note = body.note if body is not None else None
    now = utc_now()

    def _persist(write_session) -> None:
        write_session.add(models.LedgerReviewAck(acked_at=now, note=note))

    queued_write(_persist)
    return {"status": "acked", "note": note}


@router.get("/paper/candidates")
async def paper_candidates(session: DbSession, limit: int = 10) -> dict:
    price_source = MarketSnapshotPriceSource(session)
    result = build_candidates(session, n=limit, now=utc_now(), price_source=price_source)
    return {
        "candidates": [
            {key: _json_value(value) for key, value in candidate.items()}
            for candidate in result["candidates"]
        ],
        "excluded_no_evidence": result["excluded_no_evidence"],
        "generated_at": result["generated_at"],
        **scores_validation_status(),
    }


@router.post("/paper/candidates/{mint}/trade")
async def trade_candidate(mint: str, body: CandidateTradeBody, session: DbSession) -> dict:
    price_source = MarketSnapshotPriceSource(session)
    candidate = get_candidate(session, mint, now=utc_now(), price_source=price_source)
    if candidate is None:
        raise HTTPException(422, {"error": "candidate_not_found"})
    try:
        position_id = TradeEngine(price_source, now=utc_now).open_trade(
            session,
            candidate=candidate,
            mode=body.mode,
            why_entered=body.why_entered,
        )
    except (TradeRefused, RulesIncomplete) as exc:
        _raise_mapped(exc)
    return {"status": "opened", "position_id": position_id}


@router.get("/paper/signals/attribution")
async def signal_attribution(session: DbSession) -> list[dict]:
    trades = session.scalars(
        select(models.LedgerTrade).order_by(models.LedgerTrade.closed_at)
    ).all()
    groups: dict[str, list[models.LedgerTrade]] = {}
    for trade in trades:
        signal_type = (trade.source_signal or "unknown").split(":", 1)[0] or "unknown"
        groups.setdefault(signal_type, []).append(trade)
    out: list[dict] = []
    for signal_type, rows in sorted(groups.items()):
        wins = sum(1 for row in rows if row.pnl_sol is not None and row.pnl_sol > 0)
        if len(rows) < 10:
            out.append({"signal_type": signal_type, "trades": len(rows), "insufficient_data": True})
            continue
        values = [row.pnl_sol for row in rows if row.pnl_sol is not None]
        mean = sum(values, Decimal("0")) / len(values) if values else Decimal("0")
        out.append(
            {
                "signal_type": signal_type,
                "trades": len(rows),
                "wins": wins,
                "mean_pnl_sol": str(mean),
            }
        )
    return out


# Live trading routes (Phase D2-D4)


class ArmBody(BaseModel):
    confirm: str


class ExecuteBody(BaseModel):
    mint: str
    chain: str
    side: str
    size_sol: Decimal
    slippage_pct: Decimal
    priority_fee_sol: Decimal
    why: str
    source_signal: str | None = None
    position_id: int | None = None


@router.post("/trading/arm")
async def trading_arm(body: ArmBody, session: DbSession) -> dict:
    from trading.live_gate import ARM_PHRASE

    if body.confirm != ARM_PHRASE:
        raise HTTPException(422, {"error": "wrong_arm_phrase"})
    settings = get_settings()
    if settings.live_trading_enabled != "1":
        raise HTTPException(403, {"error": "env_flag_off"})
    now = utc_now()

    def _persist(write_session) -> None:
        write_session.add(models.LiveGateState(ts=now, kind="armed", reason="manual_arm"))

    queued_write(_persist)
    return {"status": "armed", **scores_validation_status()}


@router.get("/trading/status")
async def trading_status(session: DbSession) -> dict:
    from trading.live_gate import daily_loss_sol, is_open

    settings = get_settings()
    loss = daily_loss_sol(session)
    return {
        "env_flag": settings.live_trading_enabled == "1",
        "gate_open": is_open(session),
        "daily_loss_sol": str(loss),
        "daily_loss_limit_sol": (
            None
            if settings.live_daily_loss_limit_sol is None
            else str(settings.live_daily_loss_limit_sol)
        ),
        "live_max_bankroll_sol": (
            None if settings.live_max_bankroll_sol is None else str(settings.live_max_bankroll_sol)
        ),
        **scores_validation_status(),
    }


@router.post("/trading/kill")
async def trading_kill(session: DbSession) -> dict:
    from trading.gmgn_exec import GmgnExecutor

    now = utc_now()

    def _persist(write_session) -> None:
        write_session.add(models.LiveGateState(ts=now, kind="killed", reason="manual_kill"))

    queued_write(_persist)

    submitted = session.scalars(
        select(models.LiveFill).where(models.LiveFill.status == "submitted")
    ).all()
    cancelled = 0
    uncancelled: list[str] = []
    for fill in submitted:
        try:
            GmgnExecutor(session).cancel(fill.order_id)
            cancelled += 1
        except Exception:  # noqa: BLE001 - kill must survive any executor failure.
            uncancelled.append(fill.order_id)
    return {
        "killed": True,
        "cancelled": cancelled,
        "failed": len(uncancelled),
        "uncancelled": uncancelled,
    }


@router.post("/trading/copy/signal", status_code=202)
async def trading_copy_signal(body: CopySignalBody, session: DbSession) -> dict:
    """Queue a tracked-wallet copy signal; approval remains a separate human step."""
    from trading.copy import maybe_queue_copy

    row = maybe_queue_copy(session, body.wallet, body.mint, body.size_sol)
    if row is None:
        raise HTTPException(422, {"error": "copy_not_queued"})
    return {
        "status": row.status,
        "log_id": row.id,
        "detail": row.detail,
        **scores_validation_status(),
    }


@router.post("/trading/copy/{log_id}/approve")
async def trading_copy_approve(log_id: int, session: DbSession) -> dict:
    from trading.copy import approve_copy

    try:
        row = approve_copy(session, log_id)
    except TradeRefused as exc:
        _raise_mapped(exc)
    return {
        "status": row.status,
        "log_id": row.id,
        "detail": row.detail,
        **scores_validation_status(),
    }


@router.post("/trading/copy/{log_id}/recover")
async def trading_copy_recover(
    log_id: int, session: DbSession, body: RecoverCopyBody | None = None
) -> dict:
    """Recover a copy log left approving by a crash."""
    from trading.copy import recover_copy_approval

    payload = body or RecoverCopyBody()
    try:
        row = recover_copy_approval(
            session,
            log_id,
            confirm_no_order=payload.confirm_no_order,
            position_id=payload.position_id,
        )
    except TradeRefused as exc:
        _raise_mapped(exc)
    return {
        "status": row.status,
        "log_id": row.id,
        "detail": row.detail,
        **scores_validation_status(),
    }


@router.post("/trading/execute")
async def trading_execute(body: ExecuteBody, session: DbSession) -> dict:
    from trading.live_gate import require_open

    try:
        require_open(session)
    except LiveDisabled as exc:
        _raise_mapped(exc)

    price_source = MarketSnapshotPriceSource(session)
    engine = TradeEngine(price_source, now=utc_now)
    if body.side == "buy":
        candidate = get_candidate(session, body.mint, now=utc_now(), price_source=price_source)
        if candidate is None:
            raise HTTPException(422, {"error": "candidate_not_found"})
        try:
            position_id = engine.open_trade(
                session,
                candidate=candidate,
                mode="live",
                why_entered=body.why,
            )
        except (TradeRefused, RulesIncomplete) as exc:
            _raise_mapped(exc)
        return {"status": "buy_executed", "position_id": position_id}

    if body.side == "sell":
        if body.position_id is None:
            raise HTTPException(422, {"error": "position_id_required"})
        try:
            trade_id = engine.close_trade(
                session,
                body.position_id,
                why_exited=body.why,
                lesson="live sell",
            )
        except TradeRefused as exc:
            _raise_mapped(exc)
        return {"status": "sell_executed", "trade_id": trade_id}

    raise HTTPException(422, {"error": "bad_side"})
