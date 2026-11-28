from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backtest.runner import point_in_time_market_rows
from common.config import get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from forecast.harvest import HarvestPath, exit_window_feasible, harvest_path
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

LABEL_IGNITION = "ignition"
LABEL_COLLAPSE = "collapse"
LABEL_FORWARD_MAX_GAIN = "forward_max_gain_pct"
LABEL_FORWARD_MAX_DRAWDOWN = "forward_max_drawdown_pct"
LABEL_HOURS_TO_PEAK = "hours_to_peak"
LABEL_HOURS_TO_TROUGH = "hours_to_trough"
LABEL_EXECUTABLE_PEAK_GAIN_S02 = "executable_peak_gain_s02"
LABEL_EXECUTABLE_PEAK_GAIN_S10 = "executable_peak_gain_s10"
LABEL_EXECUTABLE_PEAK_GAIN_S25 = "executable_peak_gain_s25"
LABEL_HARVEST_WINDOW_START = "harvest_window_start_hours"
LABEL_COLLAPSE_CONFIRMED = "collapse_confirmed"
LABEL_SECOND_LEG = "second_leg"
LABEL_EXIT_WINDOW_FEASIBLE_S02 = "exit_window_feasible_s02"
LABEL_EXIT_WINDOW_FEASIBLE_S10 = "exit_window_feasible_s10"
LABEL_EXIT_WINDOW_FEASIBLE_S25 = "exit_window_feasible_s25"

_FRACTION_LABELS: tuple[tuple[float, str], ...] = (
    (0.02, LABEL_EXECUTABLE_PEAK_GAIN_S02),
    (0.10, LABEL_EXECUTABLE_PEAK_GAIN_S10),
    (0.25, LABEL_EXECUTABLE_PEAK_GAIN_S25),
)

_EXIT_WINDOW_FRACTION_LABELS: tuple[tuple[float, str], ...] = (
    (0.02, LABEL_EXIT_WINDOW_FEASIBLE_S02),
    (0.10, LABEL_EXIT_WINDOW_FEASIBLE_S10),
    (0.25, LABEL_EXIT_WINDOW_FEASIBLE_S25),
)


def forward_evidence_sufficient(
    timestamps: Iterable[datetime],
    *,
    start: datetime,
    window: timedelta,
) -> bool:
    """Whether forward snapshots earn a collapse=0 label for a window.

    A non-collapse is a claim about the whole forward window, so it is
    only as good as the observation coverage behind it: at least one
    snapshot per six hours of window (scaling with window length) and at
    least one snapshot in the window's final quartile. Snapshots outside
    ``(start, start + window]`` do not count. A collapse=1 needs no such
    density — the observed crash is itself the evidence — so callers gate
    only the zero case with this predicate.
    """
    if window <= timedelta(0):
        return False
    start_utc = ensure_utc(start)
    end = start_utc + window
    stamps = [ensure_utc(ts) for ts in timestamps if start_utc < ensure_utc(ts) <= end]
    minimum = max(1, math.ceil(window.total_seconds() / (6 * 3600)))
    if len(stamps) < minimum:
        return False
    final_quartile_start = end - window / 4
    return any(ts >= final_quartile_start for ts in stamps)


@dataclass(frozen=True)
class ForwardPath:
    max_gain_pct: float  # max(future)/entry - 1
    max_drawdown_pct: float  # min(future)/entry - 1 (<= 0)
    hours_to_peak: float  # hours from decision to argmax price
    hours_to_trough: float  # hours from decision to argmin price


def forward_path(
    entry_price: float,
    start: datetime,
    rows: list[tuple[datetime, float]],
) -> ForwardPath | None:
    """The full forward path from one decision point, in a single pass.

    Returns ``None`` when ``rows`` is empty. Peak/trough ties break to
    the earliest timestamp, so the timing fields name the first moment
    the extreme was reached.
    """
    if not rows:
        return None
    start_utc = ensure_utc(start)
    peak_ts, peak_price = ensure_utc(rows[0][0]), rows[0][1]
    trough_ts, trough_price = peak_ts, peak_price
    for raw_ts, price in rows[1:]:
        ts = ensure_utc(raw_ts)
        if price > peak_price or (price == peak_price and ts < peak_ts):
            peak_ts, peak_price = ts, price
        if price < trough_price or (price == trough_price and ts < trough_ts):
            trough_ts, trough_price = ts, price
    return ForwardPath(
        max_gain_pct=peak_price / entry_price - 1.0,
        max_drawdown_pct=trough_price / entry_price - 1.0,
        hours_to_peak=(peak_ts - start_utc).total_seconds() / 3600.0,
        hours_to_trough=(trough_ts - start_utc).total_seconds() / 3600.0,
    )


def forward_label_specs(path: ForwardPath) -> tuple[tuple[str, float, str], ...]:
    """The continuous labels a forward path persists: (type, value, count key)."""
    return (
        (LABEL_FORWARD_MAX_GAIN, path.max_gain_pct, "forward_max_gain"),
        (LABEL_FORWARD_MAX_DRAWDOWN, path.max_drawdown_pct, "forward_max_drawdown"),
        (LABEL_HOURS_TO_PEAK, path.hours_to_peak, "hours_to_peak"),
        (LABEL_HOURS_TO_TROUGH, path.hours_to_trough, "hours_to_trough"),
    )


def harvest_label_specs(path: HarvestPath) -> tuple[tuple[str, str], ...]:
    """The labels a harvest path persists: (type, preformatted value).

    Executable-peak gains are written at all three reference sizes so
    the exit evaluator never recomputes paths. The window-start label
    is absent when no row qualified — absence, not a zero. The two
    episode flags are binary ``"1"``/``"0"``.
    """
    specs: list[tuple[str, str]] = [
        (label_type, f"{path.executable_peak_gain_pct[fraction]:.6f}")
        for fraction, label_type in _FRACTION_LABELS
    ]
    if path.harvest_window_start_hours is not None:
        specs.append((LABEL_HARVEST_WINDOW_START, f"{path.harvest_window_start_hours:.6f}"))
    specs.append((LABEL_COLLAPSE_CONFIRMED, "1" if path.collapse_confirmed else "0"))
    specs.append((LABEL_SECOND_LEG, "1" if path.second_leg else "0"))
    return tuple(specs)


def exit_window_label_specs(feasible: dict[float, bool]) -> tuple[tuple[str, str], ...]:
    """The exit-window labels a feasibility verdict persists: one
    binary ``"1"``/``"0"`` per reference size — exit feasible for
    that size at its executable peak (depth, volume, and cost legs
    all holding there; see ``forecast.harvest.exit_window_feasible``).
    """
    return tuple(
        (label_type, "1" if feasible[fraction] else "0")
        for fraction, label_type in _EXIT_WINDOW_FRACTION_LABELS
    )


def point_in_time_depth_series(
    session: Session,
    *,
    asset_id: int,
    observed_by: datetime,
) -> list[tuple[datetime, float | None]]:
    """The asset's quote-depth (USD) series as known at ``observed_by``.

    Liquidity snapshots join through the asset's pairs to their pools,
    exactly as market snapshots join through pairs. The point-in-time
    gate is ``observed_at`` (and ``ts``): a depth print restated after
    the label-generation run cannot leak into a historical label. The
    depth value is ``reserve_usd`` — the USD-denominated depth series;
    ``reserve_quote`` is native quote units and would break the USD
    reference-notional math in ``forecast.harvest``.
    """
    pairs = list(session.scalars(select(models.Pair).where(models.Pair.base_asset_id == asset_id)))
    pool_ids = sorted({pair.pool_id for pair in pairs if pair.pool_id is not None})
    if not pool_ids:
        return []
    rows = session.scalars(
        select(models.LiquiditySnapshot)
        .where(
            models.LiquiditySnapshot.pool_id.in_(pool_ids),
            models.LiquiditySnapshot.ts <= observed_by,
            models.LiquiditySnapshot.observed_at <= observed_by,
        )
        .order_by(models.LiquiditySnapshot.ts, models.LiquiditySnapshot.id)
    ).all()
    return [(ensure_utc(row.ts), row.reserve_usd) for row in rows]


def aligned_harvest_rows(
    price_points: list[tuple[datetime, float]],
    depth_series: list[tuple[datetime, float | None]],
) -> list[tuple[datetime, float, float | None]]:
    """Align a price path to its depth path: each price row carries the
    last depth print at or before its ts (``None`` before the first
    print). Price points are sorted by ts — the harvest semantics
    (window intervals, drawdowns, legs) are sequence-dependent."""
    aligned: list[tuple[datetime, float, float | None]] = []
    index = 0
    current: float | None = None
    for ts, price in sorted(price_points):
        while index < len(depth_series) and depth_series[index][0] <= ts:
            current = depth_series[index][1]
            index += 1
        aligned.append((ts, price, current))
    return aligned


def aligned_exit_window_rows(
    price_volume_points: list[tuple[datetime, float, float | None]],
    depth_series: list[tuple[datetime, float | None]],
) -> list[tuple[datetime, float, float | None, float | None]]:
    """The exit-window row alignment: ``aligned_harvest_rows``' depth
    walk over price points that also carry their trailing volume, so
    each row is ``(ts, price, last depth at or before ts, the row's
    own trailing volume)`` — the exact rows
    ``forecast.harvest.exit_window_feasible`` judges."""
    aligned: list[tuple[datetime, float, float | None, float | None]] = []
    index = 0
    current: float | None = None
    for ts, price, volume in sorted(price_volume_points, key=lambda point: (point[0], point[1])):
        while index < len(depth_series) and depth_series[index][0] <= ts:
            current = depth_series[index][1]
            index += 1
        aligned.append((ts, price, current, volume))
    return aligned


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
        counts = {
            "ignition": 0,
            "collapse": 0,
            "forward_max_gain": 0,
            "forward_max_drawdown": 0,
            "hours_to_peak": 0,
            "hours_to_trough": 0,
            "harvest": 0,
            "exit_window": 0,
        }
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
                depth_series = point_in_time_depth_series(
                    session, asset_id=asset_id, observed_by=decision_ts
                )
                for index, decision_row in enumerate(rows):
                    decision = ensure_utc(decision_row.ts)
                    if decision + window > decision_ts:
                        continue
                    price = decision_row.price_usd
                    if price is None or price <= 0:
                        continue
                    future_rows = [
                        row
                        for row in rows[index + 1 :]
                        if row.price_usd is not None and ensure_utc(row.ts) <= decision + window
                    ]
                    entry = float(price)
                    future_points = [
                        (ensure_utc(row.ts), float(row.price_usd))
                        for row in future_rows
                        if row.price_usd is not None
                    ]
                    path = forward_path(entry, decision, future_points)
                    if path is None:
                        continue
                    peak_pct = path.max_gain_pct
                    trough_pct = path.max_drawdown_pct
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
                    # Continuous forward path, beside the binary labels. A
                    # gain below the ignition threshold is a "no pump" claim
                    # about the whole window, earned only by the same
                    # forward-evidence density that gates collapse=0 below;
                    # at or above the threshold the observed pump is itself
                    # the evidence, so positives are never gated. The
                    # sibling measurements (drawdown, hours to peak/trough)
                    # are written whenever the gain label is.
                    gain_claim_earned = (
                        peak_pct >= self.settings.forecast_ignition_threshold
                        or forward_evidence_sufficient(
                            [row.ts for row in future_rows],
                            start=decision,
                            window=window,
                        )
                    )
                    if gain_claim_earned:
                        for label_type, value, count_key in forward_label_specs(path):
                            if upsert_label(
                                session,
                                asset_id=asset_id,
                                ts=decision,
                                label_type=label_type,
                                label_value=f"{value:.6f}",
                                decision_ts=decision_ts,
                                source=f"forecast:{self.settings.forecast_model_version}",
                            ):
                                counts[count_key] += 1
                    # Harvest labels, beside the forward labels and BEFORE
                    # the collapse censoring below can skip the point: the
                    # executable episode is its own claim, gated only on a
                    # depth path existing (no depth path -> absence, not
                    # zeros). Written unconditionally of the forward
                    # density gate for the same reason positives are.
                    hpath = harvest_path(
                        entry,
                        decision,
                        aligned_harvest_rows(future_points, depth_series),
                        settings=self.settings,
                    )
                    if hpath is not None:
                        for label_type, label_value in harvest_label_specs(hpath):
                            if upsert_label(
                                session,
                                asset_id=asset_id,
                                ts=decision,
                                label_type=label_type,
                                label_value=label_value,
                                decision_ts=decision_ts,
                                source=f"forecast:{self.settings.forecast_model_version}",
                            ):
                                counts["harvest"] += 1
                    # Exit-window labels (Task 17), beside the harvest
                    # labels: per-size exit feasibility at each size's
                    # executable peak, judged on the same aligned
                    # episode plus each row's trailing volume. Absence
                    # when there is no depth/volume path, same rule as
                    # the harvest labels above.
                    ewin = exit_window_feasible(
                        aligned_exit_window_rows(
                            [
                                (ensure_utc(row.ts), float(row.price_usd), row.volume_usd)
                                for row in future_rows
                                if row.price_usd is not None
                            ],
                            depth_series,
                        ),
                        settings=self.settings,
                    )
                    if ewin is not None:
                        for label_type, label_value in exit_window_label_specs(ewin):
                            if upsert_label(
                                session,
                                asset_id=asset_id,
                                ts=decision,
                                label_type=label_type,
                                label_value=label_value,
                                decision_ts=decision_ts,
                                source=f"forecast:{self.settings.forecast_model_version}",
                            ):
                                counts["exit_window"] += 1
                    collapse_value = (
                        1.0 if trough_pct <= self.settings.forecast_collapse_threshold else 0.0
                    )
                    # A collapse=0 must be earned by forward-evidence density;
                    # with thin coverage the point is censored, not labelled
                    # a non-collapser. Positives are never gated.
                    if collapse_value == 0.0 and not forward_evidence_sufficient(
                        [row.ts for row in future_rows],
                        start=decision,
                        window=window,
                    ):
                        continue
                    if self._upsert_label(
                        session,
                        asset_id=asset_id,
                        ts=decision,
                        label_type=LABEL_COLLAPSE,
                        value=collapse_value,
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
    """Idempotent label upsert used by both LabelEngine and the bootstrap.

    A dense (interpolated) label never overwrites a real observed label, and
    the source marker is always updated on overwrite so it can never go stale.
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
        new_dense = source.startswith("dense-labels:")
        if new_dense and not existing_dense:
            # Interpolated label must not destroy a real observed label.
            return False
        row.label_value = label_value
        row.observed_at = decision_ts
        row.label_source = source
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
    counts = {
        "ignition": 0,
        "collapse": 0,
        "decision_points": 0,
        "forward_max_gain": 0,
        "forward_max_drawdown": 0,
        "hours_to_peak": 0,
        "hours_to_trough": 0,
        "harvest": 0,
        "exit_window": 0,
    }

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

            # Find the entry price: latest market snapshot at or before feature_ts.
            # Gated on observed_at <= decision_ts so a snapshot ingested after
            # the label-generation run cannot leak into a historical decision.
            entry_snap = session.scalar(
                select(models.MarketSnapshot)
                .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
                .where(
                    models.Pair.base_asset_id == asset_id,
                    models.MarketSnapshot.ts <= feature_ts,
                    models.MarketSnapshot.observed_at <= decision_ts,
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

            # Find future prices within the forward window — again gated on
            # observed_at so backfilled snapshots stay out of the outcome.
            future_snaps = session.scalars(
                select(models.MarketSnapshot)
                .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
                .where(
                    models.Pair.base_asset_id == asset_id,
                    models.MarketSnapshot.ts > feature_ts,
                    models.MarketSnapshot.ts <= feature_ts + window,
                    models.MarketSnapshot.observed_at <= decision_ts,
                    models.MarketSnapshot.price_usd.is_not(None),
                    models.MarketSnapshot.price_usd > 0,
                )
            ).all()
            if not future_snaps:
                continue

            future_points = [
                (ensure_utc(snap.ts), float(snap.price_usd))
                for snap in future_snaps
                if snap.price_usd is not None
            ]
            path = forward_path(entry_price, ensure_utc(feature_ts), future_points)
            if path is None:
                continue

            peak_pct = path.max_gain_pct
            trough_pct = path.max_drawdown_pct

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

            # Continuous forward path, beside the binary labels, with the
            # same symmetric censoring as LabelEngine.generate: a gain
            # below the ignition threshold is a "no pump" claim earned
            # only by forward-evidence density; positives are never gated.
            gain_claim_earned = peak_pct >= ignition_threshold or forward_evidence_sufficient(
                [snap.ts for snap in future_snaps],
                start=feature_ts,
                window=window,
            )
            if gain_claim_earned:
                for label_type, value, count_key in forward_label_specs(path):
                    if upsert_label(
                        session,
                        asset_id=asset_id,
                        ts=feature_ts,
                        label_type=label_type,
                        label_value=f"{value:.6f}",
                        decision_ts=decision_ts,
                        source=source,
                    ):
                        counts[count_key] += 1

            # Harvest labels, beside the forward labels and before the
            # collapse censoring below can skip the point — same
            # placement and gating as LabelEngine.generate (depth path
            # or absence).
            depth_series = point_in_time_depth_series(
                session, asset_id=asset_id, observed_by=decision_ts
            )
            hpath = harvest_path(
                entry_price,
                ensure_utc(feature_ts),
                aligned_harvest_rows(future_points, depth_series),
                settings=settings,
            )
            if hpath is not None:
                for label_type, label_value in harvest_label_specs(hpath):
                    if upsert_label(
                        session,
                        asset_id=asset_id,
                        ts=feature_ts,
                        label_type=label_type,
                        label_value=label_value,
                        decision_ts=decision_ts,
                        source=source,
                    ):
                        counts["harvest"] += 1

            # Exit-window labels (Task 17), beside the harvest labels —
            # same placement and absence rule as LabelEngine.generate.
            ewin = exit_window_feasible(
                aligned_exit_window_rows(
                    [
                        (ensure_utc(snap.ts), float(snap.price_usd), snap.volume_usd)
                        for snap in future_snaps
                        if snap.price_usd is not None
                    ],
                    depth_series,
                ),
                settings=settings,
            )
            if ewin is not None:
                for label_type, label_value in exit_window_label_specs(ewin):
                    if upsert_label(
                        session,
                        asset_id=asset_id,
                        ts=feature_ts,
                        label_type=label_type,
                        label_value=label_value,
                        decision_ts=decision_ts,
                        source=source,
                    ):
                        counts["exit_window"] += 1

            # Write collapse label (upsert for idempotency). A zero is
            # censored when forward evidence is too thin to earn it;
            # positives are never gated.
            collapse_value = "1" if trough_pct <= collapse_threshold else "0"
            if collapse_value == "0" and not forward_evidence_sufficient(
                [snap.ts for snap in future_snaps],
                start=feature_ts,
                window=window,
            ):
                continue
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
