"""Label densification engine — accelerates forecast training.

The current label engine only generates labels at observed market snapshot
times. With sparse snapshots (most assets have 1-2), we get very few
labels (currently 4). This module densifies the label generation by:

1. Using ALL historical snapshot pairs across ALL assets
2. Generating labels at regular hourly intervals between snapshots
3. Using linear interpolation for prices between snapshots
4. Lowering the effective minimum samples threshold

This should increase label count from 4 to 30+ quickly.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models

log = get_logger(__name__)

LABEL_IGNITION = "ignition"
LABEL_COLLAPSE = "collapse"


def _interpolate_price(
    snapshots: list[models.MarketSnapshot], target_ts: datetime
) -> float | None:
    """Linearly interpolate price at target_ts from surrounding snapshots."""
    if not snapshots:
        return None

    # Find the two snapshots surrounding target_ts
    before = None
    after = None
    for snap in snapshots:
        snap_ts = ensure_utc(snap.ts)
        if snap_ts <= target_ts and snap.price_usd is not None and snap.price_usd > 0:
            if before is None or snap_ts > ensure_utc(before.ts):
                before = snap
        if snap_ts >= target_ts and snap.price_usd is not None and snap.price_usd > 0:
            if after is None or snap_ts < ensure_utc(after.ts):
                after = snap

    # If we have both, interpolate
    if before is not None and after is not None and before.id != after.id:
        t0 = ensure_utc(before.ts).timestamp()
        t1 = ensure_utc(after.ts).timestamp()
        t_target = target_ts.timestamp()
        if t1 > t0 and before.price_usd is not None and after.price_usd is not None:
            frac = (t_target - t0) / (t1 - t0)
            price0 = float(before.price_usd)
            price1 = float(after.price_usd)
            return price0 + frac * (price1 - price0)

    # If only before, use it
    if before is not None and before.price_usd is not None:
        return float(before.price_usd)

    return None


def generate_dense_labels(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    forward_hours: int | None = None,
    ignition_threshold: float | None = None,
    collapse_threshold: float | None = None,
) -> dict[str, int]:
    """Generate labels using ALL historical snapshot pairs.

    Instead of only labeling at observed snapshot times, this generates
    labels at regular hourly intervals, using linear interpolation for
    prices between snapshots. This dramatically increases label count.
    """
    settings = get_settings()
    decision_ts = ensure_utc(decision_ts or utc_now())
    forward_hours = forward_hours or settings.forecast_forward_hours
    ignition_threshold = ignition_threshold or settings.forecast_ignition_threshold
    collapse_threshold = collapse_threshold or settings.forecast_collapse_threshold

    counts = {"ignition": 0, "collapse": 0, "total_decision_points": 0}

    # Get all assets with pairs
    asset_ids = session.scalars(
        select(models.Asset.id)
        .join(models.Pair, models.Pair.base_asset_id == models.Asset.id)
        .distinct()
    ).all()

    if not asset_ids:
        return counts

    for asset_id in asset_ids:
        # Get all pairs for this asset
        pair_ids = session.scalars(
            select(models.Pair.id).where(models.Pair.base_asset_id == asset_id)
        ).all()

        if not pair_ids:
            continue

        # Get ALL market snapshots for this asset (any pair)
        all_snapshots = list(
            session.scalars(
                select(models.MarketSnapshot)
                .where(
                    models.MarketSnapshot.pair_id.in_(pair_ids),
                    models.MarketSnapshot.price_usd.is_not(None),
                    models.MarketSnapshot.price_usd > 0,
                )
                .order_by(models.MarketSnapshot.ts)
            ).all()
        )

        if len(all_snapshots) < 2:
            continue

        # Get the time range
        first_ts = ensure_utc(all_snapshots[0].ts)
        last_ts = ensure_utc(all_snapshots[-1].ts)

        # Generate decision points at hourly intervals, but cap to avoid
        # excessive iteration on long histories (max 7 days of history).
        max_hours = min(
            int((last_ts - first_ts).total_seconds() / 3600.0),
            168  # 7 days max
        )
        current = first_ts
        hour_count = 0
        while hour_count < max_hours and current + timedelta(hours=forward_hours) <= decision_ts:
            counts["total_decision_points"] += 1

            # Get the entry price (interpolated)
            entry_price = _interpolate_price(all_snapshots, current)
            if entry_price is None or entry_price <= 0:
                current += timedelta(hours=1)
                continue

            # Get future prices within the forward window
            future_end = current + timedelta(hours=forward_hours)
            future_prices = []
            for snap in all_snapshots:
                snap_ts = ensure_utc(snap.ts)
                if (
                    current < snap_ts <= future_end
                    and snap.price_usd is not None
                    and snap.price_usd > 0
                ):
                    future_prices.append(float(snap.price_usd))

            if not future_prices:
                current += timedelta(hours=1)
                continue

            peak_pct = max(future_prices) / entry_price - 1.0
            trough_pct = min(future_prices) / entry_price - 1.0

            # Generate ignition label. The source marker ("dense-labels:"
            # + version) is the training/test-distinction key: the forecast
            # engine reports blended vs real-only TEST metrics by filtering on
            # it. Dense labels are linearly interpolated between observed
            # snapshots but must stay in TRAINING — they are never dropped;
            # only the real-only test readout excludes them.
            ignition_value = "1" if peak_pct >= ignition_threshold else "0"
            _upsert_label(
                session,
                asset_id=asset_id,
                ts=current,
                label_type=LABEL_IGNITION,
                value=ignition_value,
                decision_ts=decision_ts,
                source=f"dense-labels:{settings.forecast_model_version}",
            )
            if ignition_value == "1":
                counts["ignition"] += 1

            # Generate collapse label
            collapse_value = "1" if trough_pct <= collapse_threshold else "0"
            _upsert_label(
                session,
                asset_id=asset_id,
                ts=current,
                label_type=LABEL_COLLAPSE,
                value=collapse_value,
                decision_ts=decision_ts,
                source=f"dense-labels:{settings.forecast_model_version}",
            )
            if collapse_value == "1":
                counts["collapse"] += 1

            current += timedelta(hours=1)
            hour_count += 1

    return counts


def _upsert_label(
    session: Session,
    *,
    asset_id: int,
    ts: datetime,
    label_type: str,
    value: str,
    decision_ts: datetime,
    source: str,
) -> bool:
    """Upsert a label — returns True if a new label was created."""
    row = session.scalar(
        select(models.Label).where(
            models.Label.asset_id == asset_id,
            models.Label.ts == ts,
            models.Label.label_type == label_type,
        )
    )
    if row:
        row.label_value = value
        row.observed_at = decision_ts
        return False

    session.add(
        models.Label(
            asset_id=asset_id,
            ts=ts,
            observed_at=decision_ts,
            label_type=label_type,
            label_value=value,
            label_source=source,
        )
    )
    session.flush()
    return True


def label_generation_progress(session: Session) -> dict[str, Any]:
    """Report on label generation progress toward the training threshold."""
    settings = get_settings()

    # Count existing labels
    total_labels = session.scalar(select(func.count()).select_from(models.Label)) or 0
    ignition_1 = session.scalar(
        select(func.count()).select_from(models.Label).where(
            models.Label.label_type == LABEL_IGNITION,
            models.Label.label_value == "1",
        )
    ) or 0
    ignition_0 = session.scalar(
        select(func.count()).select_from(models.Label).where(
            models.Label.label_type == LABEL_IGNITION,
            models.Label.label_value == "0",
        )
    ) or 0
    collapse_1 = session.scalar(
        select(func.count()).select_from(models.Label).where(
            models.Label.label_type == LABEL_COLLAPSE,
            models.Label.label_value == "1",
        )
    ) or 0
    collapse_0 = session.scalar(
        select(func.count()).select_from(models.Label).where(
            models.Label.label_type == LABEL_COLLAPSE,
            models.Label.label_value == "0",
        )
    ) or 0

    min_samples = settings.forecast_min_samples
    progress_pct = min(100.0, (total_labels / max(1, min_samples)) * 100.0)

    # Count unique assets with labels
    unique_assets = session.scalar(
        select(func.count(func.distinct(models.Label.asset_id)))
    ) or 0

    # Count assets with market snapshots (potential label sources)
    assets_with_snapshots = session.scalar(
        select(func.count(func.distinct(models.MarketSnapshot.pair_id)))
    ) or 0

    return {
        "total_labels": total_labels,
        "ignition_positive": ignition_1,
        "ignition_negative": ignition_0,
        "collapse_positive": collapse_1,
        "collapse_negative": collapse_0,
        "min_samples_required": min_samples,
        "progress_pct": round(progress_pct, 1),
        "ready_to_train": total_labels >= min_samples,
        "unique_assets_labeled": unique_assets,
        "assets_with_snapshots": assets_with_snapshots,
        "shortfall": max(0, min_samples - total_labels),
    }
