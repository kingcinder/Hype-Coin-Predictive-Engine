"""Ticket 1, step 1: GREEN-then-collapsed baseline audit over prediction_records.

Measurement only — this script never writes to the database (it connects
through a SQLite ``mode=ro`` URI) and changes no engine behavior. It produces
the two baseline numbers the Ticket 1 fix plan validates every later step
against (scoping report section 4, step 1):

(a) the GREEN-then-collapsed rate, and
(b) the snapshot-age distribution at decision time, overall and for the
    GREEN-then-collapsed failures (the M1 staleness test, report section 3).

Definitions (each mirrors the live code path or the scoping report):

* Population: ``prediction_records`` — the one store that records GREEN
  verdicts (report section 1.5) — with ``scored_at`` in the last ``--days``
  days ending at ``--until`` (default: the newest ``scored_at`` in the
  table). Report section 4, step 1 fixes the window at 14 days.
* Live at score time: the asset's lifecycle phase at ``scored_at`` — the
  latest ``lifecycle_events`` row with ``ts <= scored_at`` and
  ``observed_at <= scored_at`` (``features/factory.py::_lifecycle_phase``) —
  is one of the pre-collapse phases (rank < 4) in
  ``validation/harness.py``'s ``LIVE_PHASES``. Assets with no event at or
  before ``scored_at`` are "unknown" and excluded from the live cohort (the
  harness assigns unknown to neither stratum).
* Snapshot age / entry price: the market snapshot the feature factory would
  have used — pairs of the asset created at or before ``scored_at``
  (``features/factory.py::_load_pit_market_inputs``), snapshots with
  ``ts <= scored_at`` and ``observed_at <= scored_at``, latest by the
  factory's ``_market_sort_key``. Age is ``scored_at - snapshot.ts``
  (report section 3, M1); the entry price is that snapshot's ``price_usd``
  (``compute_market_block``'s current price).
* Collapsed: the observed trough ``price_usd`` over the asset's snapshots in
  ``(scored_at, scored_at + --window-hours]`` falls at least ``--drawdown``
  below the entry price. Report section 4, step 1 fixes the defaults at
  48h / 50%. This is the audit's own outcome definition — deliberately NOT
  the forecast label threshold (``forecast_collapse_threshold = -0.70``
  over 24h), which report section 1.8 warns not to conflate with it.
* Evaluable: entry price present and positive, and at least one forward
  snapshot with a usable price exists in the window (mirrors the label
  engines' skip-only-on-zero-future-snapshots rule, report section 1.8).
  The rate is collapsed / evaluable; censored predictions are counted,
  never dropped silently.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

# Pre-collapse lifecycle phases (rank < 4): validation/harness.py LIVE_PHASES.
LIVE_PHASES = frozenset({"seeding", "ignition", "parabolic", "saturation"})

DEFAULT_DAYS = 14
DEFAULT_WINDOW_HOURS = 48.0
DEFAULT_DRAWDOWN = 0.50


@dataclass(frozen=True)
class Prediction:
    asset_id: int
    scored_at: datetime
    risk_band: str


@dataclass(frozen=True)
class Snapshot:
    pair_id: int
    source_id: int
    ts: datetime
    observed_at: datetime
    price_usd: float | None
    volume_usd: float | None
    buys: float | None
    sells: float | None


@dataclass(frozen=True)
class LifecycleMark:
    phase: str
    ts: datetime
    observed_at: datetime


@dataclass(frozen=True)
class PredictionAudit:
    risk_band: str
    live: bool | None
    snapshot_age_hours: float | None
    evaluable: bool
    collapsed: bool | None
    censor_reason: str | None


def parse_timestamp(value: object) -> datetime:
    """Parse a stored timestamp into an aware UTC datetime.

    Accepts datetimes, epoch seconds, and the ISO strings SQLAlchemy writes
    into SQLite (space or ``T`` separator, optional fraction/offset). Naive
    values are read as UTC — the engine writes UTC everywhere.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    else:
        raise TypeError(f"unsupported timestamp value: {type(value).__name__}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (the numpy.percentile default method)."""
    if not sorted_values:
        raise ValueError("percentile of an empty sequence")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * (q / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(sorted_values[int(rank)])
    weight = rank - low
    return float(sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight)


def _age_block(values: Sequence[float]) -> dict[str, object]:
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "p50": None, "p90": None, "p99": None, "max": None}
    return {
        "n": len(ordered),
        "p50": percentile(ordered, 50),
        "p90": percentile(ordered, 90),
        "p99": percentile(ordered, 99),
        "max": float(ordered[-1]),
    }


def _market_sort_key(row: Snapshot) -> tuple:
    """Mirror of features/factory.py::_market_sort_key (latest = max)."""
    return (
        row.ts,
        row.source_id,
        row.price_usd if row.price_usd is not None else float("-inf"),
        row.volume_usd if row.volume_usd is not None else float("-inf"),
        row.buys if row.buys is not None else -1.0,
        row.sells if row.sells is not None else -1.0,
        row.observed_at,
    )


def phase_at_score(lifecycle: Sequence[LifecycleMark], scored_at: datetime) -> str | None:
    """Lifecycle phase in force at ``scored_at`` (factory PIT filters)."""
    candidates = [
        mark for mark in lifecycle if mark.ts <= scored_at and mark.observed_at <= scored_at
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda mark: (mark.ts, mark.observed_at))
    return latest.phase.strip().lower()


def evaluate_prediction(
    prediction: Prediction,
    snapshots: Sequence[Snapshot],
    pair_created_at: Mapping[int, datetime],
    lifecycle: Sequence[LifecycleMark],
    *,
    window_hours: float,
    drawdown: float,
) -> PredictionAudit:
    """Score one prediction against the audit definitions (pure, no I/O)."""
    scored_at = prediction.scored_at
    eligible = [
        row
        for row in snapshots
        if row.ts <= scored_at
        and row.observed_at <= scored_at
        and pair_created_at.get(row.pair_id) is not None
        and pair_created_at[row.pair_id] <= scored_at
    ]
    latest = max(eligible, key=_market_sort_key) if eligible else None
    age_hours = (scored_at - latest.ts).total_seconds() / 3600.0 if latest is not None else None
    entry_price = latest.price_usd if latest is not None else None

    phase = phase_at_score(lifecycle, scored_at)
    live = None if phase is None else phase in LIVE_PHASES

    evaluable = False
    collapsed: bool | None = None
    censor_reason: str | None = None
    if prediction.risk_band.strip().upper() == "GREEN" and live:
        if entry_price is None or entry_price <= 0:
            censor_reason = "no_entry_price"
        else:
            window_end = scored_at + timedelta(hours=window_hours)
            forward_prices = [
                row.price_usd
                for row in snapshots
                if scored_at < row.ts <= window_end
                and row.price_usd is not None
                and row.price_usd > 0
            ]
            if not forward_prices:
                censor_reason = "no_forward_evidence"
            else:
                evaluable = True
                collapsed = min(forward_prices) <= entry_price * (1.0 - drawdown)
    return PredictionAudit(
        risk_band=prediction.risk_band,
        live=live,
        snapshot_age_hours=age_hours,
        evaluable=evaluable,
        collapsed=collapsed,
        censor_reason=censor_reason,
    )


def summarize_audits(audits: Sequence[PredictionAudit]) -> dict[str, object]:
    """Fold per-prediction audits into the headline baseline numbers."""
    green = [a for a in audits if a.risk_band.strip().upper() == "GREEN"]
    green_live = [a for a in green if a.live]
    evaluable = [a for a in green_live if a.evaluable]
    collapsed = [a for a in evaluable if a.collapsed]
    return {
        "predictions_total": len(audits),
        "green_total": len(green),
        "green_live": len(green_live),
        "green_phase_unknown": len([a for a in green if a.live is None]),
        "green_live_evaluable": len(evaluable),
        "green_live_no_entry_price": len(
            [a for a in green_live if a.censor_reason == "no_entry_price"]
        ),
        "green_live_no_forward_evidence": len(
            [a for a in green_live if a.censor_reason == "no_forward_evidence"]
        ),
        "green_then_collapsed": len(collapsed),
        "green_then_collapsed_rate": (len(collapsed) / len(evaluable) if evaluable else None),
        "predictions_without_snapshot": len([a for a in audits if a.snapshot_age_hours is None]),
        "snapshot_age_hours": {
            "overall": _age_block(
                [a.snapshot_age_hours for a in audits if a.snapshot_age_hours is not None]
            ),
            "green_live": _age_block(
                [a.snapshot_age_hours for a in green_live if a.snapshot_age_hours is not None]
            ),
            "green_then_collapsed": _age_block(
                [a.snapshot_age_hours for a in collapsed if a.snapshot_age_hours is not None]
            ),
        },
    }


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError(f"expected a numeric value, got {type(value).__name__}")


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError(f"expected a number, got {type(value).__name__}")


def run_audit(
    db_path: Path,
    *,
    days: int = DEFAULT_DAYS,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    drawdown: float = DEFAULT_DRAWDOWN,
    until: datetime | None = None,
) -> dict[str, object]:
    """Run the audit against the SQLite DB at ``db_path`` (read-only)."""
    connection = _connect_readonly(db_path)
    try:
        # Only the three needed columns are read; timestamps are parsed in
        # Python so the audit does not depend on one storage serialization.
        rows = connection.execute(
            "SELECT asset_id, scored_at, risk_band FROM prediction_records"
        ).fetchall()
        predictions = [
            Prediction(
                asset_id=int(row[0]), scored_at=parse_timestamp(row[1]), risk_band=str(row[2])
            )
            for row in rows
        ]
        summary: dict[str, object] = {
            "db": str(db_path),
            "days": days,
            "window_hours": window_hours,
            "drawdown": drawdown,
            "window_start": None,
            "window_end": None,
        }
        if not predictions:
            summary.update(summarize_audits([]))
            return summary

        anchor = until if until is not None else max(p.scored_at for p in predictions)
        window_start = anchor - timedelta(days=days)
        scoped = [p for p in predictions if window_start <= p.scored_at <= anchor]

        by_asset: dict[int, list[Prediction]] = defaultdict(list)
        for prediction in scoped:
            by_asset[prediction.asset_id].append(prediction)

        audits: list[PredictionAudit] = []
        for asset_id, asset_predictions in sorted(by_asset.items()):
            pair_created_at = {
                int(row[0]): parse_timestamp(row[1])
                for row in connection.execute(
                    "SELECT id, created_at FROM pairs WHERE base_asset_id = ?",
                    (asset_id,),
                )
            }
            snapshots: list[Snapshot] = []
            if pair_created_at:
                placeholders = ", ".join("?" for _ in pair_created_at)
                snapshot_rows = connection.execute(
                    "SELECT pair_id, source_id, ts, observed_at, price_usd,"
                    " volume_usd, buys, sells FROM market_snapshots"
                    f" WHERE pair_id IN ({placeholders})",
                    tuple(pair_created_at),
                )
                snapshots = [
                    Snapshot(
                        pair_id=int(row[0]),
                        source_id=int(row[1]),
                        ts=parse_timestamp(row[2]),
                        observed_at=parse_timestamp(row[3]),
                        price_usd=_optional_float(row[4]),
                        volume_usd=_optional_float(row[5]),
                        buys=_optional_float(row[6]),
                        sells=_optional_float(row[7]),
                    )
                    for row in snapshot_rows
                ]
            lifecycle = [
                LifecycleMark(
                    phase=str(row[0]),
                    ts=parse_timestamp(row[1]),
                    observed_at=parse_timestamp(row[2]),
                )
                for row in connection.execute(
                    "SELECT phase, ts, observed_at FROM lifecycle_events WHERE asset_id = ?",
                    (asset_id,),
                )
            ]
            for prediction in asset_predictions:
                audits.append(
                    evaluate_prediction(
                        prediction,
                        snapshots,
                        pair_created_at,
                        lifecycle,
                        window_hours=window_hours,
                        drawdown=drawdown,
                    )
                )
        summary.update(summarize_audits(audits))
        summary["window_start"] = window_start
        summary["window_end"] = anchor
        return summary
    finally:
        connection.close()


def _format_age_block(label: str, block: Mapping[str, object]) -> str:
    if not block["n"]:
        return f"snapshot_age_hours[{label}]: n=0"
    return (
        f"snapshot_age_hours[{label}]: n={block['n']}"
        f" p50={_as_float(block['p50']):.2f} p90={_as_float(block['p90']):.2f}"
        f" p99={_as_float(block['p99']):.2f} max={_as_float(block['max']):.2f}"
    )


def format_summary(summary: Mapping[str, object]) -> str:
    """Render the audit summary as concise plain text."""
    lines = ["ticket1 green-collapse audit (read-only, mode=ro)"]
    lines.append(f"db: {summary['db']}")
    if summary["window_end"] is None:
        lines.append("window: (no prediction_records)")
    else:
        lines.append(
            f"window: {summary['window_start']} .. {summary['window_end']} ({summary['days']}d)"
        )
    lines.append(
        f"params: forward_window={summary['window_hours']}h"
        f" drawdown>={_as_float(summary['drawdown']):.0%}"
        " live=lifecycle phase rank<4"
    )
    lines.append(f"predictions_total: {summary['predictions_total']}")
    lines.append(f"green_total: {summary['green_total']}")
    lines.append(f"green_live_at_score: {summary['green_live']}")
    lines.append(f"green_phase_unknown: {summary['green_phase_unknown']}")
    lines.append(f"green_live_evaluable: {summary['green_live_evaluable']}")
    lines.append(f"green_live_no_entry_price: {summary['green_live_no_entry_price']}")
    lines.append(f"green_live_no_forward_evidence: {summary['green_live_no_forward_evidence']}")
    lines.append(f"green_then_collapsed: {summary['green_then_collapsed']}")
    rate = summary["green_then_collapsed_rate"]
    if isinstance(rate, (int, float)):
        lines.append(f"green_then_collapsed_rate: {rate:.4f} (of evaluable)")
    else:
        lines.append("green_then_collapsed_rate: n/a (no evaluable)")
    lines.append(f"predictions_without_snapshot: {summary['predictions_without_snapshot']}")
    age_blocks = summary["snapshot_age_hours"]
    assert isinstance(age_blocks, Mapping)
    for label in ("overall", "green_live", "green_then_collapsed"):
        block = age_blocks[label]
        assert isinstance(block, Mapping)
        lines.append(_format_age_block(label, block))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path, help="path to the engine SQLite DB (opened read-only)")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="audit window in days (default: 14, per scoping report section 4 step 1)",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help="forward collapse window in hours (default: 48)",
    )
    parser.add_argument(
        "--drawdown",
        type=float,
        default=DEFAULT_DRAWDOWN,
        help="collapse drawdown fraction from the score-time price (default: 0.50)",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="ISO timestamp anchoring the window end (default: max scored_at in the DB)",
    )
    args = parser.parse_args(argv)
    if not args.db.exists():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 2
    until = parse_timestamp(args.until) if args.until else None
    try:
        summary = run_audit(
            args.db,
            days=args.days,
            window_hours=args.window_hours,
            drawdown=args.drawdown,
            until=until,
        )
    except sqlite3.Error as exc:
        print(f"error: could not audit {args.db}: {exc}", file=sys.stderr)
        return 2
    print(format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
