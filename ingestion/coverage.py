"""Snapshot-coverage measurement (Task 0 precondition).

Everything downstream — forward labels, replay, the entry/exit gates —
presumes forward windows are actually observed. The label density rule
(``forecast.labels.forward_evidence_sufficient``) requires at least
``ceil(window / 6h)`` snapshots in the forward window (4 for the 24h
window), one of them in the final quartile. Measured on the live DB
(2026-11-28), ``market_snapshots`` held ~3.4 points per asset over 5 days
and 99.1% of forecasted assets were unevaluable for forward gains: pairs
were snapshotted when polled as pairs, but the forecasted/scored set was
never guaranteed a snapshot cadence.

This module measures that gap with the *same* rule the labels use — no
parallel, weaker coverage definition. A decision point is in scope when
its full forward window has elapsed by ``as_of``; the scope is the most
recent window of such decision points (decisions in
``[as_of - 2*window, as_of - window]``), so a daily report judges the
decision points whose windows accrued under the current cadence.

Point-in-time discipline: a snapshot counts only when it was *observed*
by ``as_of`` (``observed_at <= as_of``) — a backfilled row must not
retroactively make a thin window dense. Decision rows are held to the
same rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import Settings
from common.time import ensure_utc
from forecast.labels import forward_evidence_sufficient
from storage import models


@dataclass(frozen=True)
class CoverageReport:
    as_of: datetime
    window_hours: int
    assets_measured: int  # forecasted/scored assets with a decision point in scope
    assets_meeting_density: int  # assets with >= 1 decision point passing the label rule
    pass_share: float  # assets_meeting_density / assets_measured (0.0 when none)
    median_snapshots_per_asset_day: float
    p10_snapshots_per_asset_day: float


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy's default method)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + frac * (ordered[high] - ordered[low])


def _decision_points(
    session: Session,
    *,
    scope_start: datetime,
    scope_end: datetime,
    as_of: datetime,
) -> dict[int, list[datetime]]:
    """Distinct (asset, decision_ts) points from forecasts ∪ scores in scope."""
    points: dict[int, set[datetime]] = {}
    for model in (models.Forecast, models.Score):
        rows = session.execute(
            select(model.asset_id, model.decision_ts).where(
                model.decision_ts >= scope_start,
                model.decision_ts <= scope_end,
                model.observed_at <= as_of,
            )
        ).all()
        for asset_id, decision_ts in rows:
            points.setdefault(asset_id, set()).add(ensure_utc(decision_ts))
    return {asset_id: sorted(stamps) for asset_id, stamps in points.items()}


def _snapshot_timestamps(
    session: Session,
    *,
    asset_ids: list[int],
    since: datetime,
    as_of: datetime,
) -> dict[int, list[datetime]]:
    """Snapshot ts per asset (all pairs, all sources), observed by ``as_of``."""
    rows = session.execute(
        select(models.Pair.base_asset_id, models.MarketSnapshot.ts)
        .join(models.MarketSnapshot, models.MarketSnapshot.pair_id == models.Pair.id)
        .where(
            models.Pair.base_asset_id.in_(asset_ids),
            models.MarketSnapshot.ts > since,
            models.MarketSnapshot.ts <= as_of,
            models.MarketSnapshot.observed_at <= as_of,
        )
    ).all()
    stamps: dict[int, list[datetime]] = {}
    for asset_id, ts in rows:
        stamps.setdefault(asset_id, []).append(ensure_utc(ts))
    return stamps


def snapshot_coverage(session: Session, *, settings: Settings, as_of: datetime) -> CoverageReport:
    """Measure forward-window snapshot density for forecasted/scored assets.

    Density is judged per decision point with the label rule itself:
    ``forward_evidence_sufficient(snapshot_ts, start=decision_ts,
    window=timedelta(hours=settings.forecast_forward_hours))``. An asset
    meets density when at least one of its in-scope decision points
    passes. ``median/p10_snapshots_per_asset_day`` count each measured
    asset's snapshots over the measured span (scope start .. ``as_of``).
    """
    as_of = ensure_utc(as_of)
    window = timedelta(hours=settings.forecast_forward_hours)
    scope_end = as_of - window
    scope_start = scope_end - window

    points = _decision_points(session, scope_start=scope_start, scope_end=scope_end, as_of=as_of)
    asset_ids = sorted(points)
    if not asset_ids:
        return CoverageReport(
            as_of=as_of,
            window_hours=settings.forecast_forward_hours,
            assets_measured=0,
            assets_meeting_density=0,
            pass_share=0.0,
            median_snapshots_per_asset_day=0.0,
            p10_snapshots_per_asset_day=0.0,
        )

    stamps = _snapshot_timestamps(session, asset_ids=asset_ids, since=scope_start, as_of=as_of)
    span_days = (as_of - scope_start).total_seconds() / 86400.0
    meeting = 0
    per_day: list[float] = []
    for asset_id in asset_ids:
        asset_stamps = stamps.get(asset_id, [])
        per_day.append(len(asset_stamps) / span_days if span_days > 0 else 0.0)
        if any(
            forward_evidence_sufficient(asset_stamps, start=decision_ts, window=window)
            for decision_ts in points[asset_id]
        ):
            meeting += 1

    return CoverageReport(
        as_of=as_of,
        window_hours=settings.forecast_forward_hours,
        assets_measured=len(asset_ids),
        assets_meeting_density=meeting,
        pass_share=meeting / len(asset_ids),
        median_snapshots_per_asset_day=_percentile(per_day, 50),
        p10_snapshots_per_asset_day=_percentile(per_day, 10),
    )
