from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import AlertState, AlertType
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health, upsert_prelaunch_candidate

log = get_logger(__name__)


class PrelaunchQueue:
    """Ranks tokens before they have a tradable pool.

    The alpha edge is pre-listing awareness: a token ranked before its pool exists
    is a token the radar is already watching at t0. Drivers are narrative velocity
    on cheap public channels, proximity of scheduled catalysts (TGE/airdrop/unlock),
    dev presence, and syndicate recidivism (a known pump-and-dump syndicate behind a
    launch is a near-certain collapse, so it ranks high *and* gets flagged).
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def scan(
        self, session: Session, *, decision_ts: datetime | None = None
    ) -> list[models.PrelaunchCandidate]:
        decision_ts = ensure_utc(decision_ts or utc_now())
        output: list[models.PrelaunchCandidate] = []
        try:
            with_pair = select(models.Pair.base_asset_id).distinct()
            assets = session.scalars(
                select(models.Asset).where(
                    ~models.Asset.id.in_(with_pair),
                )
            ).all()
            for asset in assets:
                candidate = self._rank_asset(session, asset, decision_ts)
                if candidate:
                    output.append(candidate)
            record_health(
                session,
                component="prelaunch_queue",
                state="ok",
                message=f"{len(output)} prelaunch candidates ranked",
            )
        except Exception as exc:  # noqa: BLE001 - preserve exact queue failure.
            log.exception("prelaunch_scan_failed", error=str(exc))
            record_health(
                session,
                component="prelaunch_queue",
                state="red",
                message=str(exc),
                error_count=1,
            )
        return output

    def _rank_asset(
        self, session: Session, asset: models.Asset, decision_ts: datetime
    ) -> models.PrelaunchCandidate | None:
        mentions_24h = (
            session.scalar(
                select(func.count())
                .select_from(models.SocialMention)
                .where(
                    models.SocialMention.observed_at <= decision_ts,
                    models.SocialMention.ts > decision_ts - timedelta(hours=24),
                    (models.SocialMention.asset_id == asset.id)
                    | (models.SocialMention.topic.ilike(f"%{asset.symbol}%")),
                )
            )
            or 0
        )
        total_mentions = (
            session.scalar(
                select(func.count())
                .select_from(models.SocialMention)
                .where(
                    models.SocialMention.observed_at <= decision_ts,
                    (models.SocialMention.asset_id == asset.id)
                    | (models.SocialMention.topic.ilike(f"%{asset.symbol}%")),
                )
            )
            or 0
        )
        proximity_hours = self._catalyst_proximity(session, asset.id, decision_ts)
        recidivism = self._recidivism(session, asset.id, decision_ts)
        dev_present = 1.0 if (asset.website_url or asset.github_url) else 0.0

        narrative = _clamp(float(mentions_24h) * 10.0)
        catalyst_boost = (
            _clamp(100.0 - proximity_hours * 2.0) if proximity_hours is not None else 0.0
        )
        dev_signal = 100.0 if dev_present else 30.0
        recidivism_boost = _clamp(recidivism * 0.5)
        priority = _clamp(
            0.30 * narrative + 0.25 * catalyst_boost + 0.20 * dev_signal + 0.25 * recidivism_boost
        )
        drivers: dict[str, Any] = {
            "mentions_24h": mentions_24h,
            "total_mentions": total_mentions,
            "catalyst_proximity_hours": proximity_hours,
            "recidivism_score": round(recidivism, 2),
            "dev_present": dev_present,
        }
        candidate = upsert_prelaunch_candidate(
            session,
            asset_id=asset.id,
            decision_ts=decision_ts,
            priority_score=round(priority, 4),
            drivers=drivers,
            model_version=self.settings.prelaunch_model_version,
        )
        if priority >= self.settings.prelaunch_alert_threshold:
            self._maybe_alert(session, asset, candidate)
        return candidate

    def _catalyst_proximity(
        self, session: Session, asset_id: int, decision_ts: datetime
    ) -> float | None:
        nearest = session.scalar(
            select(func.min(models.Catalyst.scheduled_at)).where(
                models.Catalyst.asset_id == asset_id,
                models.Catalyst.scheduled_at >= decision_ts,
                models.Catalyst.observed_at <= decision_ts,
            )
        )
        if nearest is None:
            return None
        return max(0.0, (nearest - decision_ts).total_seconds() / 3600.0)

    def _recidivism(self, session: Session, asset_id: int, decision_ts: datetime) -> float:
        assessment = session.scalar(
            select(models.FingerprintAssessment)
            .where(
                models.FingerprintAssessment.asset_id == asset_id,
                models.FingerprintAssessment.decision_ts <= decision_ts,
            )
            .order_by(models.FingerprintAssessment.decision_ts.desc())
            .limit(1)
        )
        if not assessment:
            return 0.0
        return float(assessment.recidivism_score)

    def _maybe_alert(
        self, session: Session, asset: models.Asset, candidate: models.PrelaunchCandidate
    ) -> None:
        ref = f"prelaunch:{candidate.id}"
        existing = session.scalar(
            select(models.Alert).where(
                models.Alert.asset_id == asset.id,
                models.Alert.alert_type == AlertType.PRELAUNCH_CANDIDATE.value,
                models.Alert.score_snapshot_ref == ref,
            )
        )
        if existing:
            return
        from ops.alert_quality import alert_generation_allowed

        if not alert_generation_allowed(
            session, AlertType.PRELAUNCH_CANDIDATE.value, self.settings
        ):
            return
        session.add(
            models.Alert(
                asset_id=asset.id,
                alert_type=AlertType.PRELAUNCH_CANDIDATE.value,
                threshold_version=self.settings.prelaunch_model_version,
                score_snapshot_ref=ref,
                state=AlertState.OPEN.value,
                message=(
                    f"Prelaunch candidate {asset.symbol} ranked "
                    f"{candidate.priority_score:.0f}/100 before any tradable pool. "
                    f"Drivers: {candidate.drivers}"
                ),
            )
        )


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))
