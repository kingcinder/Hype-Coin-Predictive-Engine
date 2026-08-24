from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backtest.runner import point_in_time_market_rows
from common.config import get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

LABEL_IGNITION = "ignition"
LABEL_COLLAPSE = "collapse"


class LabelEngine:
    """Generates point-in-time outcome labels from persisted market history.

    A label for decision time ``d`` is only written when the full forward window
    (``d + forecast_forward_hours``) is in the past relative to the generation
    time, so training data never leaks the future.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def generate(self, session: Session, *, decision_ts: datetime | None = None) -> dict[str, int]:
        decision_ts = ensure_utc(decision_ts or utc_now())
        counts = {"ignition": 0, "collapse": 0}
        try:
            asset_ids = session.scalars(
                select(models.Asset.id)
                .join(models.Pair, models.Pair.base_asset_id == models.Asset.id)
                .distinct()
            ).all()
            window = timedelta(hours=self.settings.forecast_forward_hours)
            for asset_id in asset_ids:
                rows = point_in_time_market_rows(
                    session, asset_id=asset_id, decision_ts=decision_ts
                )
                rows = [
                    row
                    for row in rows
                    if row.price_usd is not None and row.price_usd > 0
                ]
                for index, decision_row in enumerate(rows):
                    decision = ensure_utc(decision_row.ts)
                    if decision + window > decision_ts:
                        continue
                    price = decision_row.price_usd
                    if price is None or price <= 0:
                        continue
                    future = [
                        float(row.price_usd)
                        for row in rows[index + 1 :]
                        if row.price_usd is not None
                        and ensure_utc(row.ts) <= decision + window
                    ]
                    if not future:
                        continue
                    entry = float(price)
                    peak_pct = max(future) / entry - 1.0
                    trough_pct = min(future) / entry - 1.0
                    if self._upsert_label(
                        session,
                        asset_id=asset_id,
                        ts=decision,
                        label_type=LABEL_IGNITION,
                        value=1.0 if peak_pct >= self.settings.forecast_ignition_threshold else 0.0,
                        decision_ts=decision_ts,
                        source=f"forecast:{self.settings.forecast_model_version}",
                    ):
                        counts["ignition"] += 1
                    if self._upsert_label(
                        session,
                        asset_id=asset_id,
                        ts=decision,
                        label_type=LABEL_COLLAPSE,
                        value=1.0
                        if trough_pct <= self.settings.forecast_collapse_threshold
                        else 0.0,
                        decision_ts=decision_ts,
                        source=f"forecast:{self.settings.forecast_model_version}",
                    ):
                        counts["collapse"] += 1
            record_health(
                session,
                component="forecast_labels",
                state="ok",
                message=f"{counts} labels",
            )
        except Exception as exc:  # noqa: BLE001 - preserve exact label failure.
            log.exception("forecast_labels_failed", error=str(exc))
            record_health(
                session,
                component="forecast_labels",
                state="red",
                message=str(exc),
                error_count=1,
            )
        return counts

    def _upsert_label(
        self,
        session: Session,
        *,
        asset_id: int,
        ts: datetime,
        label_type: str,
        value: float,
        decision_ts: datetime,
        source: str,
    ) -> bool:
        row = session.scalar(
            select(models.Label).where(
                models.Label.asset_id == asset_id,
                models.Label.ts == ts,
                models.Label.label_type == label_type,
            )
        )
        label_value = "1" if value >= 0.5 else "0"
        if row:
            row.label_value = label_value
            row.observed_at = decision_ts
            return False
        session.add(
            models.Label(
                asset_id=asset_id,
                ts=ts,
                observed_at=decision_ts,
                label_type=label_type,
                label_value=label_value,
                label_source=source,
            )
        )
        session.flush()
        return True
