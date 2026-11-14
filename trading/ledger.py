from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from storage import models
from storage.repository import queued_write
from trading.costs import (
    HUNDRED,
    position_size_sol,
    q_sol,
    simulate_buy,
    simulate_sell,
    slippage_pct,
)
from trading.errors import DrawdownReview, LiveDisabled, TradeRefused
from trading.executors import PriceQuote, PriceSource
from trading.rules import load_rules, validate_for_open

ZERO = Decimal("0")
STALE_PRICE_SECONDS = 60


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TradeEngine:
    """Paper and live share one engine; only the executor differs."""

    def __init__(
        self,
        price_source: PriceSource,
        settings=None,
        now=None,
    ) -> None:
        self.price_source = price_source
        self.settings = settings or get_settings()
        self._now = now or _utc_now

    # ── bankroll ────────────────────────────────────────────────────────
    def _base_bankroll(self, mode: str) -> Decimal:
        if mode == "live":
            limit = self.settings.live_max_bankroll_sol
            if limit is None:
                raise LiveDisabled("live_max_bankroll_unset")
            return limit
        return self.settings.paper_bankroll_sol

    def bankroll(self, session: Session, mode: str) -> Decimal:
        base = self._base_bankroll(mode)
        total = session.scalar(
            select(func.coalesce(func.sum(models.LedgerTrade.pnl_sol), 0)).where(
                models.LedgerTrade.mode == mode
            )
        )
        return base + Decimal(str(total or 0))

    def peak_bankroll(self, session: Session, mode: str) -> Decimal:
        base = self._base_bankroll(mode)
        peak = base
        balance = base
        trades = session.scalars(
            select(models.LedgerTrade)
            .where(models.LedgerTrade.mode == mode)
            .order_by(models.LedgerTrade.closed_at)
        ).all()
        for trade in trades:
            balance += trade.pnl_sol if trade.pnl_sol is not None else ZERO
            peak = max(peak, balance)
        return peak

    # ── open ────────────────────────────────────────────────────────────
    def open_trade(
        self,
        session: Session,
        *,
        candidate: dict,
        mode: str,
        why_entered: str,
        executor_confirm=None,
    ) -> int:
        if mode not in ("paper", "live"):
            raise TradeRefused("bad_mode")
        if mode == "live":
            from trading.live_gate import require_open  # noqa: PLC0415 - Phase D module.

            require_open(session)
        if not why_entered.strip():
            raise TradeRefused("why_required")

        rules = load_rules()
        validate_for_open(rules)
        assert rules.stop_pct is not None  # validate_for_open guarantees it.

        evidence_ts = candidate.get("evidence_ts")
        if evidence_ts is None:
            raise TradeRefused("no_evidence_ts")
        mint = candidate.get("mint")
        chain = candidate.get("chain")
        if not mint or not chain:
            raise TradeRefused("missing_candidate_field")

        quote: PriceQuote | None = self.price_source.price(mint, chain)
        if quote is None:
            raise TradeRefused("no_live_price")
        now = self._now()
        if (now - quote.ts).total_seconds() > STALE_PRICE_SECONDS:
            raise TradeRefused("stale_price")

        bankroll = self.bankroll(session, mode)
        peak = self.peak_bankroll(session, mode)
        if peak > 0 and (bankroll - peak) / peak * HUNDRED <= self.settings.drawdown_review_pct:
            last_closed = session.scalar(
                select(func.max(models.LedgerTrade.closed_at)).where(
                    models.LedgerTrade.mode == mode
                )
            )
            last_ack = session.scalar(select(func.max(models.LedgerReviewAck.acked_at)))
            if not (last_ack is not None and last_closed is not None and last_ack > last_closed):
                raise DrawdownReview()

        age_minutes = candidate.get("age_minutes")
        is_new = age_minutes is None or age_minutes < self.settings.new_token_age_minutes
        slip = slippage_pct(
            is_new,
            self.settings.slippage_pct_normal,
            self.settings.slippage_pct_new_token,
        )
        if mode == "live":
            from trading.live_gate import effective_risk_pct  # noqa: PLC0415

            risk_pct = min(self.settings.risk_per_trade_pct, effective_risk_pct())
        else:
            risk_pct = self.settings.risk_per_trade_pct
        size = position_size_sol(bankroll=bankroll, risk_pct=risk_pct, stop_pct=rules.stop_pct)
        if mode == "live" and self.settings.live_max_bankroll_sol is not None:
            if size > self.settings.live_max_bankroll_sol:
                raise TradeRefused("size_over_live_cap")

        fill = simulate_buy(
            size_sol=size,
            quote_price=quote.price,
            slip_pct=slip,
            fee_pct=self.settings.gmgn_fee_pct_per_tx,
            priority_sol=self.settings.priority_fee_sol,
        )
        if mode == "live":
            from trading.gmgn_exec import GmgnExecutor  # noqa: PLC0415 - Phase D module.

            executor = executor_confirm or GmgnExecutor(session)
            real = executor.buy(
                mint=mint,
                chain=chain,
                size_sol=size,
                slippage_pct=slip,
                priority_fee_sol=self.settings.priority_fee_sol,
                expected_price=quote.price,
            )
            if real.status != "filled" or real.price <= 0:
                raise TradeRefused("fill_unconfirmed")
            fill = simulate_buy(
                size_sol=real.size_sol,
                quote_price=real.price,
                slip_pct=ZERO,
                fee_pct=ZERO,
                priority_sol=real.fee_sol,
            )
        stop_price = quote.price * (Decimal(1) - rules.stop_pct / HUNDRED)
        target_price = (
            quote.price * (Decimal(1) + rules.target_pct / HUNDRED)
            if rules.target_pct is not None
            else None
        )
        time_stop_at = (
            now + timedelta(minutes=rules.time_stop_minutes)
            if rules.time_stop_minutes is not None
            else None
        )

        def _persist(write_session: Session) -> int:
            position = models.LedgerOpenPosition(
                mode=mode,
                mint=mint,
                chain=chain,
                source_signal=candidate.get("signal_id") or "candidate",
                entry_quote_price=quote.price,
                entry_effective_price=fill.effective_price,
                qty=fill.qty,
                size_sol=size,
                entry_fees_sol=fill.fee_sol,
                entry_slip_pct=slip,
                is_new_token=is_new,
                stop_pct=rules.stop_pct,
                target_pct=rules.target_pct,
                time_stop_minutes=rules.time_stop_minutes,
                stop_price=stop_price,
                target_price=target_price,
                time_stop_at=time_stop_at,
                opened_at=now,
                price_source=quote.source,
                price_source_ts=quote.ts,
                candidate_evidence_ts=evidence_ts,
                why_entered=why_entered,
                rules_sha256=rules.sha256,
                created_at=now,
            )
            write_session.add(position)
            write_session.flush()
            write_session.add(
                models.PriceEvidence(
                    mint=mint,
                    chain=chain,
                    side="buy",
                    price=quote.price,
                    price_source=quote.source,
                    source_ts=quote.ts,
                    fetched_at=now,
                    trade_ref=str(position.id),
                )
            )
            return position.id

        return queued_write(_persist)

    # ── close ───────────────────────────────────────────────────────────
    def close_trade(
        self,
        session: Session,
        position_id: int,
        *,
        why_exited: str,
        lesson: str,
        tx_signature: str | None = None,
    ) -> int:
        if not why_exited.strip() or not lesson.strip():
            raise TradeRefused("why_required")
        position = session.get(models.LedgerOpenPosition, position_id)
        if position is None:
            raise TradeRefused("no_such_position")

        quote: PriceQuote | None = self.price_source.price(position.mint, position.chain)
        if quote is None:
            raise TradeRefused("no_live_price")
        now = self._now()
        if (now - quote.ts).total_seconds() > STALE_PRICE_SECONDS:
            raise TradeRefused("stale_price")

        proceeds, sfill = simulate_sell(
            qty=position.qty,
            quote_price=quote.price,
            slip_pct=position.entry_slip_pct,
            fee_pct=self.settings.gmgn_fee_pct_per_tx,
            priority_sol=self.settings.priority_fee_sol,
        )
        if position.mode == "live":
            from trading.gmgn_exec import GmgnExecutor  # noqa: PLC0415 - Phase D module.

            real = GmgnExecutor(session).sell(
                mint=position.mint,
                chain=position.chain,
                size_sol=position.size_sol,
                slippage_pct=position.entry_slip_pct,
                priority_fee_sol=self.settings.priority_fee_sol,
                expected_price=quote.price,
            )
            if real.status != "filled":
                raise TradeRefused("fill_unconfirmed")
            proceeds = q_sol(real.size_sol - real.fee_sol)
        pnl = q_sol(proceeds - position.size_sol)
        fees = q_sol(position.entry_fees_sol + sfill.fee_sol + 2 * self.settings.priority_fee_sol)
        slip = position.entry_slip_pct
        slippage_sol = q_sol(
            position.size_sol * slip / HUNDRED + (position.qty * quote.price) * slip / HUNDRED
        )

        def _persist(write_session: Session) -> int:
            trade = models.LedgerTrade(
                mode=position.mode,
                opened_at=position.opened_at,
                closed_at=now,
                mint=position.mint,
                chain=position.chain,
                source_signal=position.source_signal,
                entry_price=position.entry_effective_price,
                size_sol=position.size_sol,
                fees_sol=fees,
                slippage_sol=slippage_sol,
                stop_pct=position.stop_pct,
                target_pct=position.target_pct,
                time_stop_minutes=position.time_stop_minutes,
                exit_price=quote.price,
                pnl_sol=pnl,
                tx_signature=tx_signature,
                why_entered=position.why_entered,
                why_exited=why_exited,
                lesson=lesson,
                candidate_evidence_ts=position.candidate_evidence_ts,
                price_source=quote.source,
                price_source_ts=quote.ts,
                rules_sha256=position.rules_sha256,
                created_at=now,
            )
            old = write_session.get(models.LedgerOpenPosition, position_id)
            if old is None:
                raise TradeRefused("no_such_position")
            write_session.delete(old)
            write_session.add(trade)
            write_session.flush()
            write_session.add(
                models.PriceEvidence(
                    mint=position.mint,
                    chain=position.chain,
                    side="sell",
                    price=quote.price,
                    price_source=quote.source,
                    source_ts=quote.ts,
                    fetched_at=now,
                    trade_ref=str(trade.id),
                )
            )
            return trade.id

        return queued_write(_persist)

    # ── exits ───────────────────────────────────────────────────────────
    def suggest_exits(self, session: Session) -> list[dict]:
        now = self._now()
        out: list[dict] = []
        positions = session.scalars(select(models.LedgerOpenPosition)).all()
        for position in positions:
            quote = self.price_source.price(position.mint, position.chain)
            if quote is None:
                out.append(
                    {
                        "position_id": position.id,
                        "reason": None,
                        "quote_price": None,
                        "price_unavailable": True,
                    }
                )
                continue
            reason = None
            if quote.price <= position.stop_price:
                reason = "stop"
            elif position.target_price is not None and quote.price >= position.target_price:
                reason = "target"
            elif position.time_stop_at is not None and now >= position.time_stop_at:
                reason = "time_stop"
            if reason is not None:
                out.append(
                    {
                        "position_id": position.id,
                        "reason": reason,
                        "quote_price": quote.price,
                        "price_unavailable": False,
                    }
                )
        return out
