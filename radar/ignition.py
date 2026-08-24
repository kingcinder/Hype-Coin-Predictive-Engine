from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import AlertState, AlertType, IgnitionEventType
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

IGNITION_TYPES = (
    IgnitionEventType.FIRST_LIQUIDITY_INJECTION.value,
    IgnitionEventType.SNIPER_BURST.value,
)


class IgnitionRadar:
    """Detects t0 ignition events and collapse precursors from persisted evidence.

    Everything is derived from rows where ``observed_at <= decision_ts`` so scans
    stay replay-safe: a backtest at an earlier decision time sees exactly what the
    radar would have seen then.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def scan(self, session: Session, *, decision_ts: datetime | None = None) -> dict[str, int]:
        decision_ts = ensure_utc(decision_ts or utc_now())
        counts = {
            "first_liquidity_injection": 0,
            "sniper_burst": 0,
            "liquidity_withdrawal": 0,
        }
        try:
            assets = session.scalars(
                select(models.Asset)
                .join(models.Pair, models.Pair.base_asset_id == models.Asset.id)
                .distinct()
            ).all()
            for asset in assets:
                if self._first_liquidity_injection(session, asset, decision_ts):
                    counts["first_liquidity_injection"] += 1
                if self._sniper_burst(session, asset, decision_ts):
                    counts["sniper_burst"] += 1
                if self._liquidity_withdrawal(session, asset, decision_ts):
                    counts["liquidity_withdrawal"] += 1
            record_health(
                session,
                component="radar",
                state="ok",
                message=f"ignition scan: {counts}",
                freshness_sec=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - preserve exact radar failure.
            log.exception("radar_scan_failed", error=str(exc))
            record_health(
                session,
                component="radar",
                state="red",
                message=str(exc),
                error_count=1,
            )
        return counts

    # ------------------------------------------------------------------ ignition

    def _first_liquidity_injection(
        self, session: Session, asset: models.Asset, decision_ts: datetime
    ) -> bool:
        pools = self._asset_pools(session, asset.id)
        detected = False
        for pool in pools:
            created = pool.created_at_source
            if not created:
                continue
            age_hours = (decision_ts - ensure_utc(created)).total_seconds() / 3600.0
            if age_hours > self.settings.radar_ignition_pool_age_hours:
                continue
            first = session.scalar(
                select(models.LiquiditySnapshot)
                .where(
                    models.LiquiditySnapshot.pool_id == pool.id,
                    models.LiquiditySnapshot.observed_at <= decision_ts,
                )
                .order_by(models.LiquiditySnapshot.ts)
                .limit(1)
            )
            if first is None or first.reserve_usd is None:
                continue
            if first.reserve_usd < self.settings.min_ignition_liquidity_usd:
                continue
            confidence = 0.5
            if first.reserve_usd >= self.settings.min_liquid_momentum_usd:
                confidence = 0.85
            details: dict[str, Any] = {
                "reserve_usd": round(float(first.reserve_usd), 2),
                "pool_address": pool.address,
                "chain": self._chain_slug(session, asset.chain_id),
                "pool_age_hours": round(age_hours, 3),
                "threshold_usd": self.settings.min_ignition_liquidity_usd,
            }
            if self.upsert_event(
                session,
                asset=asset,
                source_id=first.source_id,
                event_type=IgnitionEventType.FIRST_LIQUIDITY_INJECTION.value,
                ts=first.ts,
                observed_at=decision_ts,
                confidence=confidence,
                details=details,
            ):
                detected = True
        return detected

    def _sniper_burst(self, session: Session, asset: models.Asset, decision_ts: datetime) -> bool:
        pools = self._asset_pools(session, asset.id)
        pair_ids = self._asset_pair_ids(session, asset.id)
        if not pools or not pair_ids:
            return False
        created_times = [
            created for created in (pool.created_at_source for pool in pools) if created
        ]
        if not created_times:
            return False
        anchor = min(ensure_utc(created) for created in created_times)
        window_start = anchor
        window_end = min(
            decision_ts, anchor + timedelta(hours=self.settings.radar_sniper_window_hours)
        )
        if window_end <= window_start:
            return False

        buys, sells = self._sum_txns(
            session, pair_ids=pair_ids, start=window_start, end=window_end, decision_ts=decision_ts
        )
        if buys < self.settings.radar_sniper_min_buys:
            return False
        ratio = buys / max(1.0, float(sells))
        if ratio < self.settings.radar_sniper_min_buy_sell_ratio:
            return False
        confidence = min(
            0.9, 0.4 + 0.08 * math.log2(max(buys, 2.0)) + 0.05 * (ratio - 2.5)
        )
        details: dict[str, Any] = {
            "buys": buys,
            "sells": sells,
            "buy_sell_ratio": round(ratio, 3),
            "window_hours": round((window_end - window_start).total_seconds() / 3600.0, 3),
            "pool_address": min(pool.address for pool in pools),
            "chain": self._chain_slug(session, asset.chain_id),
            "window_start": window_start.isoformat(),
        }
        source_id = session.scalar(
            select(models.MarketSnapshot.source_id)
            .where(models.MarketSnapshot.pair_id.in_(pair_ids))
            .limit(1)
        )
        return self.upsert_event(
            session,
            asset=asset,
            source_id=source_id or 0,
            event_type=IgnitionEventType.SNIPER_BURST.value,
            ts=anchor,
            observed_at=decision_ts,
            confidence=round(confidence, 4),
            details=details,
        )

    # -------------------------------------------------------------- withdrawals

    def _liquidity_withdrawal(
        self, session: Session, asset: models.Asset, decision_ts: datetime
    ) -> bool:
        pools = self._asset_pools(session, asset.id)
        pair_ids = self._asset_pair_ids(session, asset.id)
        detected = False
        window = timedelta(hours=self.settings.radar_withdrawal_window_hours)
        for pool in pools:
            rows = session.scalars(
                select(models.LiquiditySnapshot)
                .where(
                    models.LiquiditySnapshot.pool_id == pool.id,
                    models.LiquiditySnapshot.observed_at <= decision_ts,
                    models.LiquiditySnapshot.reserve_usd.is_not(None),
                )
                .order_by(models.LiquiditySnapshot.ts)
            ).all()
            for previous, current in zip(rows, rows[1:], strict=False):
                if current.ts - previous.ts > window:
                    continue
                prev_usd = float(previous.reserve_usd or 0.0)
                curr_usd = float(current.reserve_usd or 0.0)
                if prev_usd <= 0:
                    continue
                drop_pct = (prev_usd - curr_usd) / prev_usd
                if drop_pct < self.settings.radar_withdrawal_drop_pct:
                    continue
                outflow = prev_usd - curr_usd
                volume = self._volume_between(
                    session, pair_ids=pair_ids, start=previous.ts, end=current.ts,
                    decision_ts=decision_ts,
                )
                if volume >= outflow * self.settings.radar_withdrawal_volume_fraction:
                    continue
                confidence = min(
                    0.9,
                    0.5 + 0.4 * ((drop_pct - self.settings.radar_withdrawal_drop_pct) / 0.5),
                )
                details: dict[str, Any] = {
                    "previous_reserve_usd": round(prev_usd, 2),
                    "current_reserve_usd": round(curr_usd, 2),
                    "drop_pct": round(drop_pct * 100.0, 2),
                    "window_volume_usd": round(volume, 2),
                    "pool_address": pool.address,
                    "chain": self._chain_slug(session, asset.chain_id),
                    "book_emptied": curr_usd < self.settings.black_min_liquidity_usd,
                }
                if self.upsert_event(
                    session,
                    asset=asset,
                    source_id=current.source_id,
                    event_type=IgnitionEventType.LIQUIDITY_WITHDRAWAL.value,
                    ts=current.ts,
                    observed_at=decision_ts,
                    confidence=round(confidence, 4),
                    details=details,
                ):
                    detected = True
        return detected

    # ------------------------------------------------------------------ helpers

    def upsert_event(
        self,
        session: Session,
        *,
        asset: models.Asset,
        source_id: int,
        event_type: str,
        ts: datetime,
        observed_at: datetime,
        confidence: float,
        details: dict[str, Any],
    ) -> bool:
        if source_id <= 0:
            return False
        existing = session.scalar(
            select(models.IgnitionEvent).where(
                models.IgnitionEvent.asset_id == asset.id,
                models.IgnitionEvent.event_type == event_type,
                models.IgnitionEvent.ts == ts,
                models.IgnitionEvent.source_id == source_id,
            )
        )
        if existing:
            return False
        event = models.IgnitionEvent(
            asset_id=asset.id,
            source_id=source_id,
            event_type=event_type,
            ts=ts,
            observed_at=observed_at,
            confidence=confidence,
            details=details,
        )
        session.add(event)
        session.flush()
        self._maybe_alert(session, asset=asset, event=event)
        return True

    def _maybe_alert(
        self, session: Session, *, asset: models.Asset, event: models.IgnitionEvent
    ) -> None:
        if event.event_type == IgnitionEventType.LIQUIDITY_WITHDRAWAL.value:
            alert_type = AlertType.LIQUIDITY_WITHDRAWAL.value
            message = (
                f"Liquidity withdrawal detected for {asset.symbol}: "
                f"-{event.details.get('drop_pct', 0.0)}% in "
                f"{event.details.get('window_volume_usd', 0.0):,} USD of window volume."
            )
        else:
            alert_type = AlertType.IGNITION_DETECTED.value
            label = (
                "sniper burst"
                if event.event_type == IgnitionEventType.SNIPER_BURST.value
                else "first liquidity injection"
            )
            message = f"Ignition signal for {asset.symbol}: {label} at {event.ts.isoformat()}."
        ref = f"ignition_event:{event.id}"
        existing = session.scalar(
            select(models.Alert).where(
                models.Alert.asset_id == asset.id,
                models.Alert.alert_type == alert_type,
                models.Alert.score_snapshot_ref == ref,
            )
        )
        if existing:
            return
        session.add(
            models.Alert(
                asset_id=asset.id,
                alert_type=alert_type,
                threshold_version=self.settings.model_version,
                score_snapshot_ref=ref,
                state=AlertState.OPEN.value,
                message=message,
            )
        )

    def _asset_pools(self, session: Session, asset_id: int) -> list[models.Pool]:
        return list(
            session.scalars(
                select(models.Pool)
                .join(models.Pair, models.Pair.pool_id == models.Pool.id)
                .where(models.Pair.base_asset_id == asset_id)
                .distinct()
            )
        )

    def _asset_pair_ids(self, session: Session, asset_id: int) -> list[int]:
        return list(
            session.scalars(
                select(models.Pair.id).where(models.Pair.base_asset_id == asset_id)
            )
        )

    def _sum_txns(
        self,
        session: Session,
        *,
        pair_ids: list[int],
        start: datetime,
        end: datetime,
        decision_ts: datetime,
    ) -> tuple[int, int]:
        buys = session.scalar(
            select(func.coalesce(func.sum(models.MarketSnapshot.buys), 0)).where(
                models.MarketSnapshot.pair_id.in_(pair_ids),
                models.MarketSnapshot.ts >= start,
                models.MarketSnapshot.ts <= end,
                models.MarketSnapshot.observed_at <= decision_ts,
            )
        )
        sells = session.scalar(
            select(func.coalesce(func.sum(models.MarketSnapshot.sells), 0)).where(
                models.MarketSnapshot.pair_id.in_(pair_ids),
                models.MarketSnapshot.ts >= start,
                models.MarketSnapshot.ts <= end,
                models.MarketSnapshot.observed_at <= decision_ts,
            )
        )
        return int(buys or 0), int(sells or 0)

    def _volume_between(
        self,
        session: Session,
        *,
        pair_ids: list[int],
        start: datetime,
        end: datetime,
        decision_ts: datetime,
    ) -> float:
        if not pair_ids:
            return 0.0
        value = session.scalar(
            select(func.coalesce(func.sum(models.MarketSnapshot.volume_usd), 0.0)).where(
                models.MarketSnapshot.pair_id.in_(pair_ids),
                models.MarketSnapshot.ts > start,
                models.MarketSnapshot.ts <= end,
                models.MarketSnapshot.observed_at <= decision_ts,
            )
        )
        return float(value or 0.0)

    def _chain_slug(self, session: Session, chain_id: int) -> str:
        chain = session.get(models.Chain, chain_id)
        return chain.slug if chain else "unknown"
