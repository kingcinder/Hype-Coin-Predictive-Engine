"""Label densification engine — accelerates forecast training.

The current label engine only generates labels at observed market snapshot
times. With sparse snapshots (most assets have 1-2), we get very few
labels (currently 4). This module densifies the label generation by:

1. Using ALL historical snapshot pairs across ALL assets
2. Generating labels at regular hourly intervals between snapshots
3. Using the last observed price at or before each grid point as the entry
   (point-in-time: never the future-adjacent snapshot)
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


def _interpolate_price(snapshots: list[models.MarketSnapshot], target_ts: datetime) -> float | None:
    """Point-in-time entry price at target_ts: last snapshot AT OR BEFORE it.

    The dense-label entry is the denominator of peak/trough percentages, so it
    must be knowable at the label's reference point — never interpolated from
    the immediately-FOLLOWING snapshot, which would pull the entry toward a
    price that hadn't been observed yet and understate the true magnitude.
    This is target-side label noise, not feature leakage, but it makes the
    already-soft dense labels softer, so the entry is computed strictly
    backward (last observed price at or before the reference point).

    Returns ``None`` when no snapshot at or before ``target_ts`` exists (the
    caller skips the grid point — no fabricated entry price).
    """
    if not snapshots:
        return None

    # Latest snapshot observed at or before the reference point (LOCF).
    # BOTH the market timestamp and the ingestion timestamp must be at or
    # before the reference point: a snapshot ingested after the grid point
    # (observed_at > target_ts) was not knowable then, even if its market ts
    # is older. Using it would understate the true entry magnitude with
    # information from the future. (Synthetic snapshots without observed_at
    # fall back to ts; persisted rows always carry observed_at.)
    before = None
    for snap in snapshots:
        snap_ts = ensure_utc(snap.ts)
        snap_observed = ensure_utc(getattr(snap, "observed_at", None) or snap.ts)
        if (
            snap_ts <= target_ts
            and snap_observed <= target_ts
            and snap.price_usd is not None
            and snap.price_usd > 0
        ):
            if before is None or snap_ts > ensure_utc(before.ts):
                before = snap

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
    labels at regular hourly intervals, using the last observed price at
    or before each grid point as the entry (point-in-time, never the
    future-adjacent snapshot). This dramatically increases label count.
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

        # Get ALL market snapshots for this asset (any pair), gated on
        # observed_at so snapshots ingested after the decision time cannot
        # leak into the interpolated labels.
        all_snapshots = list(
            session.scalars(
                select(models.MarketSnapshot)
                .where(
                    models.MarketSnapshot.pair_id.in_(pair_ids),
                    models.MarketSnapshot.observed_at <= decision_ts,
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
            168,  # 7 days max
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


def replay_historical_labels(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    forward_hours: int | None = None,
    ignition_threshold: float | None = None,
    collapse_threshold: float | None = None,
    dry_run: bool = False,
) -> dict[str, int | bool]:
    """Replay completed historical targets without using post-decision entries.

    Feature timestamps and entry prices must have been observed by the feature
    decision time. Forward outcome prices are bounded to the configured target
    horizon and must have been observed by the replay's as-of time. Labels are
    marked observed when the latest price needed to determine the outcome was
    actually observed.
    """
    settings = get_settings()
    as_of = ensure_utc(decision_ts or utc_now())
    sql_as_of = as_of.replace(tzinfo=None)
    forward_hours = forward_hours or settings.forecast_forward_hours
    ignition_threshold = (
        settings.forecast_ignition_threshold if ignition_threshold is None else ignition_threshold
    )
    collapse_threshold = (
        settings.forecast_collapse_threshold if collapse_threshold is None else collapse_threshold
    )
    window = timedelta(hours=forward_hours)
    counts: dict[str, int | bool] = {
        "decision_points": 0,
        "ignition_created": 0,
        "collapse_created": 0,
        "labels_created": 0,
        "missing_entry": 0,
        "incomplete_forward_window": 0,
        "dry_run": dry_run,
    }
    feature_points = session.execute(
        select(models.Feature.asset_id, models.Feature.decision_ts)
        .where(
            models.Feature.observed_at <= models.Feature.decision_ts,
            models.Feature.decision_ts <= sql_as_of,
        )
        .distinct()
        .order_by(models.Feature.decision_ts)
    ).all()
    for asset_id_raw, raw_feature_ts in feature_points:
        asset_id = int(asset_id_raw)
        feature_ts = ensure_utc(raw_feature_ts).replace(tzinfo=None)
        future_end = feature_ts + window
        if future_end > sql_as_of:
            continue
        counts["decision_points"] = int(counts["decision_points"]) + 1
        pair_ids = session.scalars(
            select(models.Pair.id).where(models.Pair.base_asset_id == asset_id)
        ).all()
        if not pair_ids:
            counts["missing_entry"] = int(counts["missing_entry"]) + 1
            continue
        entry = session.scalar(
            select(models.MarketSnapshot)
            .where(
                models.MarketSnapshot.pair_id.in_(pair_ids),
                models.MarketSnapshot.ts <= feature_ts,
                models.MarketSnapshot.observed_at <= feature_ts,
                models.MarketSnapshot.price_usd.is_not(None),
                models.MarketSnapshot.price_usd > 0,
            )
            .order_by(models.MarketSnapshot.ts.desc())
            .limit(1)
        )
        if entry is None or entry.price_usd is None:
            counts["missing_entry"] = int(counts["missing_entry"]) + 1
            continue
        outcomes = session.scalars(
            select(models.MarketSnapshot)
            .where(
                models.MarketSnapshot.pair_id.in_(pair_ids),
                models.MarketSnapshot.ts > feature_ts,
                models.MarketSnapshot.ts <= future_end,
                models.MarketSnapshot.observed_at <= sql_as_of,
                models.MarketSnapshot.price_usd.is_not(None),
                models.MarketSnapshot.price_usd > 0,
            )
            .order_by(models.MarketSnapshot.ts)
        ).all()
        future_prices = [float(row.price_usd) for row in outcomes if row.price_usd is not None]
        if not future_prices:
            counts["incomplete_forward_window"] = int(counts["incomplete_forward_window"]) + 1
            continue

        entry_price = float(entry.price_usd)
        ignition_value = (
            "1" if max(future_prices) / entry_price - 1.0 >= ignition_threshold else "0"
        )
        collapse_value = (
            "1" if min(future_prices) / entry_price - 1.0 <= collapse_threshold else "0"
        )
        label_observed_at = max(ensure_utc(row.observed_at) for row in [entry, *outcomes])
        source = f"historical-replay:{settings.forecast_model_version}"
        for label_type, value, count_key in (
            (LABEL_IGNITION, ignition_value, "ignition_created"),
            (LABEL_COLLAPSE, collapse_value, "collapse_created"),
        ):
            created = (
                _upsert_label(
                    session,
                    asset_id=asset_id,
                    ts=feature_ts,
                    label_type=label_type,
                    value=value,
                    decision_ts=label_observed_at,
                    source=source,
                )
                if not dry_run
                else True
            )
            if created:
                counts[count_key] = int(counts[count_key]) + 1
                counts["labels_created"] = int(counts["labels_created"]) + 1

    if not dry_run:
        session.flush()
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
    """Upsert a label — returns True if a new label was created.

    A dense (interpolated) label NEVER overwrites a real observed label:
    ground truth is not destroyed by interpolation. A real label DOES replace
    a dense one (upgrade to observed truth). The source marker is always
    updated on overwrite — previously a dense label could overwrite a real
    label's value while keeping the "real" source marker, contaminating the
    real-only test readout.
    """
    row = session.scalar(
        select(models.Label).where(
            models.Label.asset_id == asset_id,
            models.Label.ts == ts,
            models.Label.label_type == label_type,
        )
    )
    if row:
        existing_dense = str(row.label_source or "").startswith("dense-labels:")
        existing_replay = str(row.label_source or "").startswith("historical-replay:")
        new_dense = source.startswith("dense-labels:")
        new_replay = source.startswith("historical-replay:")
        if (new_dense or new_replay) and not (existing_dense or existing_replay):
            # Synthetic labels must not overwrite observed outcome labels.
            return False
        if new_dense and existing_replay:
            # The point-in-time replay supersedes the broader dense estimate.
            return False
        row.label_value = value
        row.observed_at = decision_ts
        row.label_source = source
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
    ignition_1 = (
        session.scalar(
            select(func.count())
            .select_from(models.Label)
            .where(
                models.Label.label_type == LABEL_IGNITION,
                models.Label.label_value == "1",
            )
        )
        or 0
    )
    ignition_0 = (
        session.scalar(
            select(func.count())
            .select_from(models.Label)
            .where(
                models.Label.label_type == LABEL_IGNITION,
                models.Label.label_value == "0",
            )
        )
        or 0
    )
    collapse_1 = (
        session.scalar(
            select(func.count())
            .select_from(models.Label)
            .where(
                models.Label.label_type == LABEL_COLLAPSE,
                models.Label.label_value == "1",
            )
        )
        or 0
    )
    collapse_0 = (
        session.scalar(
            select(func.count())
            .select_from(models.Label)
            .where(
                models.Label.label_type == LABEL_COLLAPSE,
                models.Label.label_value == "0",
            )
        )
        or 0
    )

    min_samples = settings.forecast_min_samples
    now = ensure_utc(utc_now()).replace(tzinfo=None)
    label_rows = session.execute(
        select(models.Label.asset_id, models.Label.ts, models.Label.label_type).where(
            models.Label.observed_at <= now,
            models.Label.ts <= now - timedelta(hours=settings.forecast_forward_hours),
            models.Label.label_type.in_((LABEL_IGNITION, LABEL_COLLAPSE)),
        )
    ).all()
    label_types_by_key: dict[tuple[int, datetime], set[str]] = {}
    for asset_id, ts, label_type in label_rows:
        label_types_by_key.setdefault((int(asset_id), ensure_utc(ts)), set()).add(label_type)
    feature_keys = {
        (int(asset_id), ensure_utc(ts))
        for asset_id, ts in session.execute(
            select(models.Feature.asset_id, models.Feature.decision_ts)
            .where(
                models.Feature.missing_flag.is_(False),
                models.Feature.observed_at <= now,
            )
            .distinct()
        ).all()
    }
    required_types = {LABEL_IGNITION, LABEL_COLLAPSE}
    usable_samples = sum(
        1
        for key, label_types in label_types_by_key.items()
        if required_types.issubset(label_types) and key in feature_keys
    )
    progress_pct = min(100.0, (usable_samples / max(1, min_samples)) * 100.0)

    # Count unique assets with labels
    unique_assets = session.scalar(select(func.count(func.distinct(models.Label.asset_id)))) or 0

    # Count assets with market snapshots (potential label sources)
    assets_with_snapshots = (
        session.scalar(select(func.count(func.distinct(models.MarketSnapshot.pair_id)))) or 0
    )

    return {
        "total_labels": total_labels,
        "ignition_positive": ignition_1,
        "ignition_negative": ignition_0,
        "collapse_positive": collapse_1,
        "collapse_negative": collapse_0,
        "min_samples_required": min_samples,
        "progress_pct": round(progress_pct, 1),
        "ready_to_train": usable_samples >= min_samples,
        "usable_forecast_samples": usable_samples,
        "forecast_status": (
            f"Forecasting live: {usable_samples}/{min_samples} labels"
            if usable_samples >= min_samples
            else f"Forecasting not live: {usable_samples}/{min_samples} labels"
        ),
        "forecast_validated": False,
        "forecast_validation_note": (
            "Unvalidated: the forecast output has not demonstrated out-of-sample edge; "
            f"{usable_samples}/{min_samples} eligible labels are available."
        ),
        "unique_assets_labeled": unique_assets,
        "assets_with_snapshots": assets_with_snapshots,
        "shortfall": max(0, min_samples - usable_samples),
    }
