from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
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
                rows = [row for row in rows if row.price_usd is not None and row.price_usd > 0]
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
                        if row.price_usd is not None and ensure_utc(row.ts) <= decision + window
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
        """Delegates to the module-level upsert to avoid duplication."""
        label_value = "1" if value >= 0.5 else "0"
        return upsert_label(
            session,
            asset_id=asset_id,
            ts=ts,
            label_type=label_type,
            label_value=label_value,
            decision_ts=decision_ts,
            source=source,
        )


def upsert_label(
    session: Session,
    *,
    asset_id: int,
    ts: datetime,
    label_type: str,
    label_value: str,
    decision_ts: datetime,
    source: str,
) -> bool:
    """Idempotent label upsert used by both LabelEngine and the bootstrap."""
    row = session.scalar(
        select(models.Label).where(
            models.Label.asset_id == asset_id,
            models.Label.ts == ts,
            models.Label.label_type == label_type,
        )
    )
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


def seed_labels_at_feature_timestamps(
    session: Session,
    *,
    decision_ts: datetime | None = None,
) -> dict[str, int]:
    """Generate labels at every timestamp where features already exist.

    The core problem: the standard label engine generates labels at observed
    market snapshot times, but the feature factory generates features at
    different (often later) timestamps.  The forecast model requires both
    labels AND features at the exact same (asset_id, ts) pair.  When they
    don't overlap, the model gets 0 training samples.

    This function fixes the gap by:
    1. Finding every (asset_id, decision_ts) pair that has features
    2. Looking up market snapshots before that timestamp to determine
       the entry price
    3. Looking up market snapshots in the forward window to determine
       the peak/trough prices
    4. Writing ignition + collapse labels at the feature timestamp

    This is the critical bridge that unblocks ML training.
    """
    settings = get_settings()
    # Strip timezone for SQLite compatibility — SQLite stores naive datetimes
    # and comparing aware vs naive silently returns no results from SQL queries.
    decision_ts = ensure_utc(decision_ts or utc_now()).replace(tzinfo=None)
    forward_hours = settings.forecast_forward_hours
    ignition_threshold = settings.forecast_ignition_threshold
    collapse_threshold = settings.forecast_collapse_threshold
    window = timedelta(hours=forward_hours)
    counts = {"ignition": 0, "collapse": 0, "decision_points": 0}

    try:
        # Find all (asset_id, decision_ts) pairs that have features but no labels
        feature_rows = session.execute(
            select(
                models.Feature.asset_id,
                models.Feature.decision_ts,
            ).distinct()
        ).all()

        for asset_id_raw, feature_ts in feature_rows:
            asset_id = int(asset_id_raw)
            # Strip timezone info for SQLite compatibility — SQLite stores naive
            # datetimes and comparing aware vs naive silently returns no results.
            feature_ts = ensure_utc(feature_ts).replace(tzinfo=None)
            # Skip if forward window hasn't elapsed yet
            if feature_ts + window > decision_ts:
                continue
            # Skip if labels already exist at this timestamp
            existing = session.scalar(
                select(func.count())
                .select_from(models.Label)
                .where(
                    models.Label.asset_id == asset_id,
                    models.Label.ts == feature_ts,
                )
            )
            if existing is not None and existing > 0:
                continue
            counts["decision_points"] += 1

            # Find the entry price: latest market snapshot at or before feature_ts
            entry_snap = session.scalar(
                select(models.MarketSnapshot)
                .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
                .where(
                    models.Pair.base_asset_id == asset_id,
                    models.MarketSnapshot.ts <= feature_ts,
                    models.MarketSnapshot.price_usd.is_not(None),
                    models.MarketSnapshot.price_usd > 0,
                )
                .order_by(models.MarketSnapshot.ts.desc())
                .limit(1)
            )
            if not entry_snap:
                continue
            price_val = entry_snap.price_usd
            if price_val is None or price_val <= 0:
                continue
            entry_price = float(price_val)

            # Find future prices within the forward window
            future_snaps = session.scalars(
                select(models.MarketSnapshot)
                .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
                .where(
                    models.Pair.base_asset_id == asset_id,
                    models.MarketSnapshot.ts > feature_ts,
                    models.MarketSnapshot.ts <= feature_ts + window,
                    models.MarketSnapshot.price_usd.is_not(None),
                    models.MarketSnapshot.price_usd > 0,
                )
            ).all()
            if not future_snaps:
                continue

            future_prices = [
                float(p) for snap in future_snaps if (p := snap.price_usd) is not None and p > 0
            ]
            if not future_prices:
                continue

            peak_pct = max(future_prices) / entry_price - 1.0
            trough_pct = min(future_prices) / entry_price - 1.0

            source = f"feature-aligned:{settings.forecast_model_version}"

            # Write ignition label (upsert for idempotency)
            ignition_value = "1" if peak_pct >= ignition_threshold else "0"
            if upsert_label(
                session,
                asset_id=asset_id,
                ts=feature_ts,
                label_type=LABEL_IGNITION,
                label_value=ignition_value,
                decision_ts=decision_ts,
                source=source,
            ):
                counts["ignition"] += 1

            # Write collapse label (upsert for idempotency)
            collapse_value = "1" if trough_pct <= collapse_threshold else "0"
            if upsert_label(
                session,
                asset_id=asset_id,
                ts=feature_ts,
                label_type=LABEL_COLLAPSE,
                label_value=collapse_value,
                decision_ts=decision_ts,
                source=source,
            ):
                counts["collapse"] += 1

        session.flush()
        record_health(
            session,
            component="forecast_labels_bootstrap",
            state="ok",
            message=f"{counts}",
        )
    except Exception as exc:  # noqa: BLE001 - bootstrap labels are additive, never block training.
        log.exception("forecast_labels_bootstrap_failed", error=str(exc))
        record_health(
            session,
            component="forecast_labels_bootstrap",
            state="red",
            message=str(exc),
            error_count=1,
        )
    return counts
