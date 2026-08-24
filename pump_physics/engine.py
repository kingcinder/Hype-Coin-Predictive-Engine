from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import AlertState, AlertType, IgnitionEventType, LifecyclePhase
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

# Monotonic lifecycle ordering. Transitions only move forward along this
# ordering (or jump to a terminal exit), so the state machine never regresses.
_PHASE_RANK = {
    LifecyclePhase.SEEDING: 0,
    LifecyclePhase.IGNITION: 1,
    LifecyclePhase.PARABOLIC: 2,
    LifecyclePhase.SATURATION: 3,
    LifecyclePhase.COLLAPSE: 4,
}
_TERMINAL = {LifecyclePhase.DEAD, LifecyclePhase.RUGGED, LifecyclePhase.SURVIVOR}

# Phases that fire a phone alert the moment they are reached: the collapse
# phase and the danger exits. SURVIVOR is intentionally excluded — it is a
# positive outcome, not a danger signal.
_ALERT_PHASES = {LifecyclePhase.COLLAPSE, LifecyclePhase.RUGGED, LifecyclePhase.DEAD}

IGNITION_WINDOW_HOURS = 24.0
PARABOLIC_VOLUME_ACCEL = 2.0
PARABOLIC_BUY_SELL = 1.3
SATURATION_BUY_SELL = 0.8
COLLAPSE_1H_RETURN_PCT = -25.0
DEAD_AFTER_NO_TRADES_HOURS = 168.0  # 7 days


@dataclass(frozen=True)
class PhaseEvidence:
    has_pool: bool
    pool_age_hours: float | None
    ignition_events: int
    withdrawal_events: int
    liquidity_usd: float | None
    volume_acceleration: float | None
    buy_sell_ratio: float | None
    holder_growth: float | None
    one_hour_return: float | None
    narrative_velocity: float | None
    last_trade_hours_ago: float | None


def detect_phase(evidence: PhaseEvidence) -> LifecyclePhase:
    """Deterministic hype-lifecycle classification from persisted evidence.

    Priority: terminal exits, then collapse, then ignition, then parabolic,
    then saturation. The scan layer enforces monotonicity on top of this, so a
    detected phase is only persisted when it advances the lifecycle.
    """
    if not evidence.has_pool:
        return LifecyclePhase.SEEDING

    if (
        evidence.withdrawal_events >= 1
        and evidence.liquidity_usd is not None
        and evidence.liquidity_usd <= 0
    ):
        return LifecyclePhase.RUGGED

    if (
        evidence.last_trade_hours_ago is not None
        and evidence.last_trade_hours_ago > DEAD_AFTER_NO_TRADES_HOURS
    ):
        return LifecyclePhase.DEAD

    if (
        evidence.one_hour_return is not None
        and evidence.one_hour_return <= COLLAPSE_1H_RETURN_PCT
    ):
        return LifecyclePhase.COLLAPSE

    if evidence.withdrawal_events >= 1:
        return LifecyclePhase.COLLAPSE

    if evidence.ignition_events >= 1:
        return LifecyclePhase.IGNITION

    if evidence.pool_age_hours is not None and evidence.pool_age_hours < IGNITION_WINDOW_HOURS:
        return LifecyclePhase.IGNITION

    if (
        evidence.volume_acceleration is not None
        and evidence.volume_acceleration >= PARABOLIC_VOLUME_ACCEL
        and evidence.buy_sell_ratio is not None
        and evidence.buy_sell_ratio >= PARABOLIC_BUY_SELL
        and (evidence.holder_growth is None or evidence.holder_growth >= 0)
    ):
        return LifecyclePhase.PARABOLIC

    if (
        evidence.buy_sell_ratio is not None
        and evidence.buy_sell_ratio < SATURATION_BUY_SELL
    ):
        return LifecyclePhase.SATURATION

    if (
        evidence.holder_growth is not None
        and evidence.holder_growth <= 0
        and evidence.one_hour_return is not None
        and evidence.one_hour_return >= 0
    ):
        return LifecyclePhase.SATURATION

    return LifecyclePhase.IGNITION


def phase_rank(phase: LifecyclePhase) -> int:
    return _PHASE_RANK.get(phase, 5)


class LifecycleEngine:
    """Hype-lifecycle state machine.

    Each tracked asset moves SEEDING -> IGNITION -> PARABOLIC -> SATURATION ->
    COLLAPSE (exits: DEAD, RUGGED, SURVIVOR). The machine consumes persisted
    point-in-time evidence — the same rows a backtest would see — and emits
    idempotent ``lifecycle_events`` when the detected phase advances. Those
    transition events are the prediction targets that the forecast layer and
    the risk engine consume.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def scan(self, session: Session, *, decision_ts: datetime | None = None) -> dict[str, int]:
        decision_ts = ensure_utc(decision_ts or utc_now())
        counts = {"events": 0, "assets": 0}
        try:
            assets = session.scalars(
                select(models.Asset)
                .outerjoin(models.Pair, models.Pair.base_asset_id == models.Asset.id)
                .outerjoin(models.SocialMention, models.SocialMention.asset_id == models.Asset.id)
                .where((models.Pair.id.is_not(None)) | (models.SocialMention.id.is_not(None)))
                .distinct()
            ).all()
            for asset in assets:
                evidence = self._evidence(session, asset, decision_ts)
                if evidence is None:
                    continue
                detected = detect_phase(evidence)
                current = self._current_phase(session, asset.id)
                if self._advances(current, detected):
                    self._emit(session, asset, detected, evidence, decision_ts)
                    counts["events"] += 1
                counts["assets"] += 1
            record_health(
                session,
                component="lifecycle",
                state="ok",
                message=f"{counts['events']} transitions across {counts['assets']} assets",
                freshness_sec=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - preserve exact lifecycle failure.
            log.exception("lifecycle_scan_failed", error=str(exc))
            record_health(
                session,
                component="lifecycle",
                state="red",
                message=str(exc),
                error_count=1,
            )
        return counts

    # ------------------------------------------------------------------ evidence

    def _evidence(
        self, session: Session, asset: models.Asset, decision_ts: datetime
    ) -> PhaseEvidence | None:
        pair_ids = list(
            session.scalars(
                select(models.Pair.id).where(models.Pair.base_asset_id == asset.id)
            )
        )
        pool_ids = list(
            session.scalars(
                select(models.Pool.id)
                .join(models.Pair, models.Pair.pool_id == models.Pool.id)
                .where(models.Pair.base_asset_id == asset.id)
                .distinct()
            )
        )
        has_pool = bool(pair_ids)

        ignition_events = 0
        withdrawal_events = 0
        for event in session.scalars(
            select(models.IgnitionEvent).where(
                models.IgnitionEvent.asset_id == asset.id,
                models.IgnitionEvent.observed_at <= decision_ts,
            )
        ).all():
            if event.event_type in (
                IgnitionEventType.FIRST_LIQUIDITY_INJECTION.value,
                IgnitionEventType.SNIPER_BURST.value,
            ):
                ignition_events += 1
            elif event.event_type == IgnitionEventType.LIQUIDITY_WITHDRAWAL.value:
                withdrawal_events += 1

        market_rows = list(
            session.scalars(
                select(models.MarketSnapshot)
                .where(
                    models.MarketSnapshot.pair_id.in_(pair_ids),
                    models.MarketSnapshot.observed_at <= decision_ts,
                )
                .order_by(models.MarketSnapshot.ts)
            )
        ) if pair_ids else []
        liquidity_rows = list(
            session.scalars(
                select(models.LiquiditySnapshot)
                .where(
                    models.LiquiditySnapshot.pool_id.in_(pool_ids),
                    models.LiquiditySnapshot.observed_at <= decision_ts,
                )
                .order_by(models.LiquiditySnapshot.ts)
            )
        ) if pool_ids else []

        narrative = session.scalar(
            select(models.SocialMention.id)
            .where(
                models.SocialMention.asset_id == asset.id,
                models.SocialMention.observed_at <= decision_ts,
                models.SocialMention.ts > decision_ts - timedelta(hours=24),
            )
            .limit(1)
        )

        if not has_pool and narrative is None:
            return None

        pool_age = None
        if pair_ids:
            created = [
                ensure_utc(row.created_at_source)
                for row in session.scalars(
                    select(models.Pool).where(models.Pool.id.in_(pool_ids))
                )
                if row.created_at_source is not None
            ]
            if created:
                pool_age = max(
                    0.0,
                    (decision_ts - min(created)).total_seconds() / 3600.0,
                )

        latest_market = market_rows[-1] if market_rows else None
        one_hour_return = None
        if latest_market is not None and latest_market.price_usd:
            entry = None
            for row in reversed(market_rows):
                if row.ts <= latest_market.ts - timedelta(hours=1) and row.price_usd:
                    entry = row
                    break
            if entry and entry.price_usd:
                one_hour_return = (
                    (float(latest_market.price_usd) / float(entry.price_usd) - 1.0) * 100.0
                )

        volume_acceleration = None
        if latest_market is not None and latest_market.volume_usd:
            prior = [row.volume_usd for row in market_rows if row.volume_usd is not None][:-1][-12:]
            if prior:
                baseline = max(1.0, sum(prior) / len(prior))
                volume_acceleration = float(latest_market.volume_usd) / baseline

        buy_sell_ratio = None
        if latest_market is not None:
            buys = float(latest_market.buys or 0.0)
            sells = float(latest_market.sells or 0.0)
            buy_sell_ratio = (buys + 1.0) / (sells + 1.0)

        holder_growth = self._holder_growth(session, asset.id, decision_ts)
        liquidity_usd = None
        if liquidity_rows and liquidity_rows[-1].reserve_usd is not None:
            liquidity_usd = float(liquidity_rows[-1].reserve_usd)
        last_trade_hours_ago = None
        if market_rows:
            last_trade_hours_ago = max(
                0.0,
                (decision_ts - ensure_utc(market_rows[-1].ts)).total_seconds() / 3600.0,
            )

        return PhaseEvidence(
            has_pool=has_pool,
            pool_age_hours=pool_age,
            ignition_events=ignition_events,
            withdrawal_events=withdrawal_events,
            liquidity_usd=liquidity_usd,
            volume_acceleration=volume_acceleration,
            buy_sell_ratio=buy_sell_ratio,
            holder_growth=holder_growth,
            one_hour_return=one_hour_return,
            narrative_velocity=(1.0 if narrative is not None else None),
            last_trade_hours_ago=last_trade_hours_ago,
        )

    def _holder_growth(
        self, session: Session, asset_id: int, decision_ts: datetime
    ) -> float | None:
        latest_ts = session.scalar(
            select(models.Holder.ts)
            .where(
                models.Holder.asset_id == asset_id,
                models.Holder.observed_at <= decision_ts,
                models.Holder.ts <= decision_ts,
            )
            .order_by(models.Holder.ts.desc())
            .limit(1)
        )
        if latest_ts is None:
            return None
        current_count = session.scalar(
            select(models.Holder.id)
            .where(models.Holder.asset_id == asset_id, models.Holder.ts == latest_ts)
            .limit(1)
        )
        if current_count is None:
            return None
        count_now = len(
            session.scalars(
                select(models.Holder.id).where(
                    models.Holder.asset_id == asset_id, models.Holder.ts == latest_ts
                )
            ).all()
        )
        prior_ts = session.scalar(
            select(models.Holder.ts)
            .where(
                models.Holder.asset_id == asset_id,
                models.Holder.ts < latest_ts,
                models.Holder.observed_at <= decision_ts,
            )
            .order_by(models.Holder.ts.desc())
            .limit(1)
        )
        if prior_ts is None:
            return None
        count_prior = len(
            session.scalars(
                select(models.Holder.id).where(
                    models.Holder.asset_id == asset_id, models.Holder.ts == prior_ts
                )
            ).all()
        )
        return float(count_now - count_prior)

    # ------------------------------------------------------------ state machine

    def _current_phase(self, session: Session, asset_id: int) -> LifecyclePhase | None:
        row = session.scalar(
            select(models.LifecycleEvent)
            .where(models.LifecycleEvent.asset_id == asset_id)
            .order_by(models.LifecycleEvent.ts.desc())
            .limit(1)
        )
        if row is None:
            return None
        try:
            return LifecyclePhase(row.phase)
        except ValueError:
            return None

    def _advances(self, current: LifecyclePhase | None, detected: LifecyclePhase) -> bool:
        if current is None:
            return True
        if current in _TERMINAL:
            return False
        if detected in _TERMINAL:
            return True
        return phase_rank(detected) > phase_rank(current)

    def _emit(
        self,
        session: Session,
        asset: models.Asset,
        phase: LifecyclePhase,
        evidence: PhaseEvidence,
        decision_ts: datetime,
    ) -> None:
        details: dict[str, Any] = {
            "pool_age_hours": evidence.pool_age_hours,
            "ignition_events": evidence.ignition_events,
            "withdrawal_events": evidence.withdrawal_events,
            "liquidity_usd": evidence.liquidity_usd,
            "volume_acceleration": evidence.volume_acceleration,
            "buy_sell_ratio": evidence.buy_sell_ratio,
            "holder_growth": evidence.holder_growth,
            "one_hour_return_pct": evidence.one_hour_return,
            "narrative_velocity": evidence.narrative_velocity,
            "last_trade_hours_ago": evidence.last_trade_hours_ago,
        }
        existing = session.scalar(
            select(models.LifecycleEvent).where(
                models.LifecycleEvent.asset_id == asset.id,
                models.LifecycleEvent.phase == phase.value,
                models.LifecycleEvent.ts == decision_ts,
                models.LifecycleEvent.event_type == "phase_transition",
            )
        )
        if existing:
            event = existing
        else:
            confidence = 0.5
            if phase in _TERMINAL:
                confidence = 0.9
            event = models.LifecycleEvent(
                asset_id=asset.id,
                phase=phase.value,
                event_type="phase_transition",
                ts=decision_ts,
                observed_at=decision_ts,
                confidence=confidence,
                details=details,
            )
            session.add(event)
            session.flush()
        if phase in _ALERT_PHASES:
            self._maybe_transition_alert(session, asset, event)

    def _maybe_transition_alert(
        self,
        session: Session,
        asset: models.Asset,
        event: models.LifecycleEvent,
    ) -> None:
        """Fire one idempotent lifecycle_transition alert per terminal event.

        Deduped on the event id (same pattern as the fingerprint alerts), so a
        token entering COLLAPSE / RUGGED / DEAD pushes once — never on every
        scan — and a missed alert is backfilled on the next scan.
        """
        ref = f"lifecycle:{event.id}"
        existing = session.scalar(
            select(models.Alert).where(
                models.Alert.asset_id == asset.id,
                models.Alert.alert_type == AlertType.LIFECYCLE_TRANSITION.value,
                models.Alert.score_snapshot_ref == ref,
            )
        )
        if existing:
            return
        details = event.details or {}
        message = (
            f"{asset.symbol} reached {event.phase.upper()} — terminal lifecycle "
            f"phase; one_hour_return={details.get('one_hour_return_pct')}, "
            f"withdrawal_events={details.get('withdrawal_events')}, "
            f"liquidity_usd={details.get('liquidity_usd')}"
        )
        session.add(
            models.Alert(
                asset_id=asset.id,
                alert_type=AlertType.LIFECYCLE_TRANSITION.value,
                threshold_version=self.settings.model_version,
                score_snapshot_ref=ref,
                state=AlertState.OPEN.value,
                message=message,
            )
        )


def run_lifecycle(
    session: Session, *, decision_ts: datetime | None = None
) -> dict[str, int]:
    return LifecycleEngine().scan(session, decision_ts=decision_ts)
