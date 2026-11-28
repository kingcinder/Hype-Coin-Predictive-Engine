#!/usr/bin/env python3
"""Depth-lead study — the Task 15 gate artifact (spec §4, build order 1).

The cheapest test in the exit programme, and it gates a premise: does
quote depth peak BEFORE the executable-value peak? Per historical
episode (an asset decision point carrying a Task 7 executable peak —
``executable_peak_gain_s10`` > 0), the study rebuilds the episode rows
exactly as Task 7's wiring builds them and measures the lead of the
depth peak over the EV peak at the f=0.10 reference (Task 7 semantics:
notional = 0.10 x the episode's PEAK quote depth, constant-product
fill). The headline is the MEDIAN lead across episodes, in snapshots
and in hours.

Exit code: 0 iff the median lead > 0, else 1. The report (JSON + MD)
is written either way.

**GATE (binding on Task 9):** Task 9's pacing layer (depth-capped
tranche sizing) may be enabled by default only when a recorded run of
this study reports median lead > 0. If the study has not run, or its
median lead is <= 0, ``exit_pacing_enabled`` ships with default False
and the tranches fall back to the ladder's fixed fractions (plan
Task 9 Step 5). The report payload carries the verdict in
``pacing_default_enabled`` so Task 9 reads it mechanically.

Usage:
  python scripts/depth_lead_study.py [--as-of <ISO-8601>] [--days 30]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from backtest.runner import point_in_time_market_rows  # noqa: E402
from common.config import Settings, get_settings  # noqa: E402
from common.time import ensure_utc, utc_now  # noqa: E402
from forecast.harvest import WINDOW_REFERENCE_FRACTION  # noqa: E402
from forecast.labels import (  # noqa: E402
    LABEL_EXECUTABLE_PEAK_GAIN_S10,
    aligned_harvest_rows,
    point_in_time_depth_series,
)
from storage import models  # noqa: E402
from storage.database import session_scope  # noqa: E402

REPORTS_DIR = REPO / "reports"

# Study lookback (days of Task 7 labels to measure). Not a policy knob:
# --days overrides it per run.
DEFAULT_WINDOW_DAYS = 30


@dataclass(frozen=True)
class EpisodeLead:
    asset_id: int
    depth_peak_ts: datetime
    ev_peak_ts: datetime  # EV at the f=0.10 reference (Task 7 semantics)
    lead_snapshots: int  # index(ev_peak) - index(depth_peak); > 0 = depth led
    lead_hours: float


def episode_lead(
    rows: list[tuple[datetime, float, float | None]],
    *,
    asset_id: int,
    settings: Settings,
) -> EpisodeLead | None:
    """The depth peak's lead over the EV peak for one episode.

    ``rows`` are Task 7's aligned harvest rows ``(ts, price,
    quote_depth_usd)`` in time order. The EV series is Task 7's: each
    row's average fill price for selling ``WINDOW_REFERENCE_FRACTION``
    x the episode's peak quote depth into that row's pool
    (``price * depth / (depth + notional)`` — ``forecast.harvest``'s
    constant-product fill). Rows with unusable depth (None or <= 0)
    host neither peak — mirroring ``harvest_path`` — but still occupy
    their index in the episode, so the lead is counted in snapshots
    as observed. Peak ties break to the earliest row, as in
    ``forecast.labels.forward_path``. Returns None when no row
    carries usable depth: without a depth path there is no episode
    to measure. (``settings`` is accepted for interface parity with
    ``harvest_path``; the reference fraction is Task 7's module
    constant, not a setting.)
    """
    if not rows:
        return None
    norm = [(ensure_utc(ts), float(price), depth) for ts, price, depth in rows]
    usable = [
        (index, ts, price, float(depth))
        for index, (ts, price, depth) in enumerate(norm)
        if depth and depth > 0
    ]
    if not usable:
        return None
    d_peak = max(depth for _, _, _, depth in usable)
    depth_peak_index, depth_peak_ts = next(
        (index, ts) for index, ts, _, depth in usable if depth == d_peak
    )
    notional = WINDOW_REFERENCE_FRACTION * d_peak
    ev_peak_index, ev_peak_ts = usable[0][0], usable[0][1]
    ev_peak = usable[0][2] * usable[0][3] / (usable[0][3] + notional)
    for index, ts, price, depth in usable[1:]:
        ev = price * depth / (depth + notional)
        if ev > ev_peak:
            ev_peak_index, ev_peak_ts, ev_peak = index, ts, ev
    return EpisodeLead(
        asset_id=asset_id,
        depth_peak_ts=depth_peak_ts,
        ev_peak_ts=ev_peak_ts,
        lead_snapshots=ev_peak_index - depth_peak_index,
        lead_hours=(ev_peak_ts - depth_peak_ts).total_seconds() / 3600.0,
    )


def median_lead(leads: list[EpisodeLead]) -> float:
    """Median of ``lead_snapshots`` across episodes.

    The median, never the mean: one episode with a freak lead must
    not carry the pacing premise. An empty study returns 0.0 — no
    episodes is no evidence of a lead, and 0.0 fails the > 0 gate,
    the conservative direction for Task 9's default.
    """
    if not leads:
        return 0.0
    return float(statistics.median(lead.lead_snapshots for lead in leads))


def collect_episode_leads(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    settings: Settings,
) -> list[EpisodeLead]:
    """Leads for every Task 7 episode whose decision falls in the window.

    An episode is a stored ``executable_peak_gain_s10`` label with
    gain > 0, observed by ``end``. Its rows are rebuilt exactly as
    Task 7's wiring builds them — point-in-time market rows as of the
    label's own generation time, the forward slice inside
    ``forecast_forward_hours``, the point-in-time depth series, and
    ``aligned_harvest_rows`` — so the study measures the same episode
    the label describes.
    """
    label_rows = session.execute(
        select(
            models.Label.asset_id,
            models.Label.ts,
            models.Label.observed_at,
            models.Label.label_value,
        ).where(models.Label.label_type == LABEL_EXECUTABLE_PEAK_GAIN_S10)
    ).all()
    window = timedelta(hours=settings.forecast_forward_hours)
    leads: list[EpisodeLead] = []
    for asset_id, raw_ts, raw_observed_at, raw_value in sorted(
        label_rows, key=lambda row: (row[0], ensure_utc(row[1]))
    ):
        try:
            gain = float(raw_value)
        except (TypeError, ValueError):
            continue
        decision = ensure_utc(raw_ts)
        observed_by = ensure_utc(raw_observed_at)
        if gain <= 0 or not (start <= decision <= end) or observed_by > end:
            continue
        market_rows = point_in_time_market_rows(session, asset_id=asset_id, decision_ts=observed_by)
        market_rows = [
            row for row in market_rows if row.price_usd is not None and row.price_usd > 0
        ]
        decision_index = next(
            (i for i, row in enumerate(market_rows) if ensure_utc(row.ts) == decision),
            None,
        )
        if decision_index is None:
            continue
        future_points = [
            (ensure_utc(row.ts), float(row.price_usd))
            for row in market_rows[decision_index + 1 :]
            if row.price_usd is not None and ensure_utc(row.ts) <= decision + window
        ]
        if not future_points:
            continue
        depth_series = point_in_time_depth_series(
            session, asset_id=asset_id, observed_by=observed_by
        )
        lead = episode_lead(
            aligned_harvest_rows(future_points, depth_series),
            asset_id=asset_id,
            settings=settings,
        )
        if lead is not None:
            leads.append(lead)
    return leads


def _render_markdown(payload: dict) -> str:
    median_hours = payload["median_lead_hours"]
    lines = [
        f"# Depth-lead study — {payload['window_end'][:10]}",
        "",
        f"- window: {payload['window_start']} .. {payload['window_end']}",
        f"- episodes (Task 7 executable peak, s10 gain > 0): {payload['n_episodes']}",
        f"- median lead: {payload['median_lead_snapshots']} snapshots, "
        + (f"{median_hours} hours" if median_hours is not None else "n/a (no episodes)"),
        f"- share of episodes with lead > 0: {payload['share_lead_positive']:.4f}",
        f"- gate: median lead > 0 -> {'PASS' if payload['passed'] else 'FAIL'}",
        "",
        "Task 9 gate: the pacing layer may ship enabled by default only on",
        "a PASS recorded here; otherwise exit_pacing_enabled defaults to",
        "False and tranches fall back to the ladder's fixed fractions.",
        "",
        "| asset | depth peak | EV peak (f=0.10) | lead (snapshots) | lead (hours) |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {episode['asset_id']} | {episode['depth_peak_ts']} | "
        f"{episode['ev_peak_ts']} | {episode['lead_snapshots']} | "
        f"{episode['lead_hours']} |"
        for episode in payload["episodes"]
    )
    lines.append("")
    return "\n".join(lines)


def run_study(
    *,
    session=None,
    settings: Settings | None = None,
    as_of: datetime | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    reports_dir: Path | None = None,
) -> int:
    """Run the depth-lead study and write the report; return the gate
    exit code (0 iff median lead > 0). The report is written either way.

    ``session`` is an injectable seam (keyword-only): when None — the
    CLI path — this call opens and owns its own ``session_scope()``
    cycle.
    """
    settings = settings if settings is not None else get_settings()
    end = ensure_utc(as_of) if as_of is not None else utc_now()
    start = end - timedelta(days=days)
    out_dir = reports_dir if reports_dir is not None else REPORTS_DIR

    if session is not None:
        leads = collect_episode_leads(session, start=start, end=end, settings=settings)
    else:
        with session_scope() as active:
            leads = collect_episode_leads(active, start=start, end=end, settings=settings)

    med_snapshots = median_lead(leads)
    med_hours = float(statistics.median(lead.lead_hours for lead in leads)) if leads else None
    passed = med_snapshots > 0
    payload = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "reference_fraction": WINDOW_REFERENCE_FRACTION,
        "n_episodes": len(leads),
        "median_lead_snapshots": med_snapshots,
        "median_lead_hours": med_hours,
        "share_lead_positive": (
            sum(1 for lead in leads if lead.lead_snapshots > 0) / len(leads) if leads else 0.0
        ),
        "passed": passed,
        "pacing_default_enabled": passed,
        "episodes": [
            {
                "asset_id": lead.asset_id,
                "depth_peak_ts": lead.depth_peak_ts.isoformat(),
                "ev_peak_ts": lead.ev_peak_ts.isoformat(),
                "lead_snapshots": lead.lead_snapshots,
                "lead_hours": lead.lead_hours,
            }
            for lead in leads
        ],
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = end.strftime("%Y%m%d")
    json_path = out_dir / f"depth-lead-study-{stamp}.json"
    md_path = out_dir / f"depth-lead-study-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    print(
        f"depth-lead study: {len(leads)} episodes, median lead "
        f"{med_snapshots} snapshots ({med_hours} hours) "
        f"-> {'PASS' if passed else 'FAIL'}; wrote {json_path} + {md_path}"
    )
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Depth-lead study (Task 15 gate)")
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 timestamp ending the study window (default: now, UTC)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="study window length in days (default: 30)",
    )
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    return run_study(as_of=as_of, days=args.days)


if __name__ == "__main__":
    sys.exit(main())
