#!/usr/bin/env python3
"""Entry-rank falsification run (mission-refactor Task 5, spec §3).

Replays the last ``--days`` (default 14) fully-observed days through the
causal replay harness (``backtest.replay.iter_replay_entries``), takes
one candidate per asset per day (the day's first replay decision point
for that asset), builds point-in-time features at the decision point via
``backtest.runner.build_replay_features`` (never persisted features —
replay stays causal even where the live feature table has gaps), and
ranks each day's cohort two ways:

- the entry composite (``scoring.entry_rank.rank_entry_cohort``), and
- a volume-only baseline (``volume_acceleration`` descending, from the
  same replay features, so both rankings see identical causal inputs).

Headline: precision@top-20/day for MFE >= 2x — the share of the day's
top 20 whose forward rows reach ``2 * entry_price`` within 6h of entry —
averaged over the window's days. Exit code 0 iff the composite's mean
precision is >= 1.5x the volume ranking's, else 1. The report is
written either way.

Three-window discipline (spec §2, principle 7): the component set was
screened on window A and the (equal-weight rank-sum) weights fixed on
window B; the replay days here are evaluation window C, disjoint from
both. The report quotes a Benjamini–Hochberg correction across the
screened components: each component's Spearman association between its
within-cohort percentile and the MFE hit, computed on window C and
quoted for the record — a window-C number is a result only for the
composite's headline comparison, never for re-screening components.

This is a research instrument: a failed falsification does not block
the composite, but the outcome belongs in STATE.md verbatim. The live
run additionally presumes the Task 0 snapshot-coverage gate — on a
sparse lake most decision points earn no replay entry and the cohorts
come back thin or empty.

Usage:
  python scripts/falsify_entry_rank.py [--as-of <ISO-8601>] [--days 14]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from backtest.replay import ReplayEntry, iter_replay_entries  # noqa: E402
from backtest.runner import build_replay_features  # noqa: E402
from common.config import Settings, get_settings  # noqa: E402
from common.time import ensure_utc, utc_now  # noqa: E402
from scoring.entry_rank import ENTRY_RANK_COMPONENTS, rank_entry_cohort  # noqa: E402
from storage import models  # noqa: E402
from storage.database import session_scope  # noqa: E402

REPORTS_DIR = REPO / "reports"

# Plan-pinned falsification constants (spec §3): the headline metric and
# its kill bar. These are the research question, not policy knobs.
TOP_K = 20
TARGET_MULTIPLE = 2.0
HORIZON_HOURS = 6
KILL_LIFT = 1.5
DEFAULT_WINDOW_DAYS = 14


@dataclass(frozen=True)
class DayResult:
    day: date
    entries: int
    composite_precision: float
    volume_precision: float


@dataclass(frozen=True)
class ComponentScreen:
    name: str
    rho: float
    p_value: float
    q_value: float  # Benjamini–Hochberg adjusted, across the screened set
    n: int


@dataclass(frozen=True)
class FalsificationReport:
    window_start: datetime
    window_end: datetime
    days: tuple[DayResult, ...]
    composite_precision: float  # mean of per-day precision@top-20
    volume_precision: float
    lift: float | None  # composite / volume; None when volume precision is 0
    passed: bool
    screening: tuple[ComponentScreen, ...]


def _mfe_hit(entry: ReplayEntry) -> bool:
    """True when the forward path reaches TARGET_MULTIPLE x entry within
    HORIZON_HOURS of entry (the plan's MFE_6h >= 2x headline event)."""
    horizon = entry.entry_ts + timedelta(hours=HORIZON_HOURS)
    target = TARGET_MULTIPLE * entry.entry_price
    return any(price >= target for ts, price in entry.forward_rows if ts <= horizon)


def _precision_at_k(ordered: list[ReplayEntry], k: int) -> float:
    if k <= 0:
        return 0.0
    return sum(1 for entry in ordered[:k] if _mfe_hit(entry)) / k


def _benjamini_hochberg(p_values: list[float]) -> list[float]:
    """Step-up BH q-values, returned in the input order."""
    n = len(p_values)
    order = sorted(range(n), key=lambda i: p_values[i])
    q_values = [1.0] * n
    running_min = 1.0
    for rank in range(n, 0, -1):
        idx = order[rank - 1]
        running_min = min(running_min, p_values[idx] * n / rank)
        q_values[idx] = min(1.0, running_min)
    return q_values


def _screen_components(
    pooled: list[tuple[dict[str, float], bool]],
) -> tuple[ComponentScreen, ...]:
    """Per-component Spearman(percentile, hit) over the pooled window-C
    cohorts, with Benjamini–Hochberg q-values across the screened set."""
    from scipy.stats import spearmanr

    rhos: list[float] = []
    p_values: list[float] = []
    for name in ENTRY_RANK_COMPONENTS:
        xs = [components[name] for components, _hit in pooled]
        ys = [1.0 if hit else 0.0 for _components, hit in pooled]
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            rho, p_value = 0.0, 1.0
        else:
            result = spearmanr(xs, ys)
            rho = float(result.statistic) if math.isfinite(result.statistic) else 0.0
            p_value = float(result.pvalue) if math.isfinite(result.pvalue) else 1.0
        rhos.append(rho)
        p_values.append(p_value)
    q_values = _benjamini_hochberg(p_values)
    return tuple(
        ComponentScreen(
            name=name, rho=rhos[i], p_value=p_values[i], q_value=q_values[i], n=len(pooled)
        )
        for i, name in enumerate(ENTRY_RANK_COMPONENTS)
    )


def evaluate_cohorts(
    cohorts: dict[date, list[tuple[ReplayEntry, dict[str, float]]]],
    *,
    settings: Settings,
    window_start: datetime,
    window_end: datetime,
) -> FalsificationReport:
    """Score pre-built day cohorts (one entry per asset per day) against
    the kill bar. Pure: no session, no I/O — the synthetic-fixture seam
    the unit tests drive."""
    day_results: list[DayResult] = []
    pooled: list[tuple[dict[str, float], bool]] = []
    for day in sorted(cohorts):
        cohort = cohorts[day]
        if not cohort:
            continue
        rows = [(entry.asset_id, features) for entry, features in cohort]
        candidates = rank_entry_cohort(rows, settings=settings)
        by_asset = {entry.asset_id: entry for entry, _features in cohort}
        composite_order = [by_asset[candidate.asset_id] for candidate in candidates]
        volume_order = [
            entry
            for entry, _features in sorted(
                cohort,
                key=lambda item: (
                    -(item[1].get("volume_acceleration", float("-inf"))),
                    item[0].asset_id,
                ),
            )
        ]
        k = min(TOP_K, len(cohort))
        day_results.append(
            DayResult(
                day=day,
                entries=len(cohort),
                composite_precision=_precision_at_k(composite_order, k),
                volume_precision=_precision_at_k(volume_order, k),
            )
        )
        candidate_by_asset = {candidate.asset_id: candidate for candidate in candidates}
        for entry, _features in cohort:
            pooled.append((candidate_by_asset[entry.asset_id].components, _mfe_hit(entry)))
    composite_precision = (
        sum(day.composite_precision for day in day_results) / len(day_results)
        if day_results
        else 0.0
    )
    volume_precision = (
        sum(day.volume_precision for day in day_results) / len(day_results) if day_results else 0.0
    )
    lift = composite_precision / volume_precision if volume_precision > 0 else None
    passed = composite_precision > 0 and composite_precision >= KILL_LIFT * volume_precision
    return FalsificationReport(
        window_start=window_start,
        window_end=window_end,
        days=tuple(day_results),
        composite_precision=composite_precision,
        volume_precision=volume_precision,
        lift=lift,
        passed=passed,
        screening=_screen_components(pooled),
    )


def _asset_ids_with_snapshots(session: Session, *, start: datetime, end: datetime) -> list[int]:
    return list(
        session.scalars(
            select(models.Pair.base_asset_id)
            .join(
                models.MarketSnapshot,
                models.MarketSnapshot.pair_id == models.Pair.id,
            )
            .where(models.MarketSnapshot.ts >= start, models.MarketSnapshot.ts <= end)
            .distinct()
        )
    )


def collect_day_cohorts(
    session: Session, *, start: datetime, end: datetime
) -> dict[date, list[tuple[ReplayEntry, dict[str, float]]]]:
    """Replay [start, end] and assemble one candidate per asset per day:
    the asset's first earned replay entry that day, with point-in-time
    features built (never persisted) at its decision point."""
    asset_ids = _asset_ids_with_snapshots(session, start=start, end=end)
    first: dict[tuple[date, int], ReplayEntry] = {}
    for entry in iter_replay_entries(session, asset_ids=asset_ids, start=start, end=end):
        first.setdefault((entry.decision_ts.date(), entry.asset_id), entry)
    by_decision: dict[datetime, list[ReplayEntry]] = {}
    for entry in first.values():
        by_decision.setdefault(entry.decision_ts, []).append(entry)
    cohorts: dict[date, list[tuple[ReplayEntry, dict[str, float]]]] = {}
    for decision_ts in sorted(by_decision):
        entries = by_decision[decision_ts]
        built = build_replay_features(
            session,
            decision_ts=decision_ts,
            asset_ids=[entry.asset_id for entry in entries],
        )
        for entry in entries:
            features = {
                name: value.value
                for name, value in built.get(entry.asset_id, {}).items()
                if not value.missing
            }
            cohorts.setdefault(entry.decision_ts.date(), []).append((entry, features))
    return cohorts


def _render_markdown(report: FalsificationReport) -> str:
    lift_text = f"{report.lift:.3f}" if report.lift is not None else "n/a (volume precision 0)"
    lines = [
        f"# Entry-rank falsification — {report.window_end.date().isoformat()}",
        "",
        f"- window C (evaluation): {report.window_start.isoformat()} .. "
        f"{report.window_end.isoformat()}",
        f"- headline: precision@top-{TOP_K}/day for MFE >= {TARGET_MULTIPLE:g}x "
        f"within {HORIZON_HOURS}h of entry",
        f"- composite precision: {report.composite_precision:.4f}",
        f"- volume-ranking precision: {report.volume_precision:.4f}",
        f"- lift: {lift_text} (kill bar: >= {KILL_LIFT:g}x)",
        f"- verdict: {'PASS' if report.passed else 'FAIL'}",
        "",
        "Three-window discipline: the component set was screened on window A",
        "and the equal-weight rank-sum fixed on window B; the days above are",
        "evaluation window C, disjoint from both. The screening table quotes",
        "each component's Spearman(percentile, hit) on window C with",
        "Benjamini-Hochberg q-values across the screened set, for the record.",
        "",
        "| day | entries | composite precision | volume precision |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {day.day.isoformat()} | {day.entries} | {day.composite_precision:.4f} "
        f"| {day.volume_precision:.4f} |"
        for day in report.days
    )
    lines.extend(
        [
            "",
            "| component | rho | p | q (BH) | n |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| {screen.name} | {screen.rho:.4f} | {screen.p_value:.4g} "
        f"| {screen.q_value:.4g} | {screen.n} |"
        for screen in report.screening
    )
    lines.append("")
    return "\n".join(lines)


def _report_payload(report: FalsificationReport) -> dict:
    return {
        "window_start": report.window_start.isoformat(),
        "window_end": report.window_end.isoformat(),
        "top_k": TOP_K,
        "target_multiple": TARGET_MULTIPLE,
        "horizon_hours": HORIZON_HOURS,
        "kill_lift": KILL_LIFT,
        "composite_precision": report.composite_precision,
        "volume_precision": report.volume_precision,
        "lift": report.lift,
        "passed": report.passed,
        "days": [
            {
                "day": day.day.isoformat(),
                "entries": day.entries,
                "composite_precision": day.composite_precision,
                "volume_precision": day.volume_precision,
            }
            for day in report.days
        ],
        "screening": [
            {
                "name": screen.name,
                "rho": screen.rho,
                "p_value": screen.p_value,
                "q_value": screen.q_value,
                "n": screen.n,
            }
            for screen in report.screening
        ],
    }


def run_falsification(
    *,
    session=None,
    settings: Settings | None = None,
    as_of: datetime | None = None,
    days: int = DEFAULT_WINDOW_DAYS,
    reports_dir: Path | None = None,
) -> int:
    """Run the falsification replay and write the report; return the
    kill-bar exit code (0 pass, 1 fail). The report is written either way.

    ``session`` is an injectable seam (keyword-only): when None — the CLI
    path — this call opens and owns its own ``session_scope()`` cycle.
    """
    settings = settings if settings is not None else get_settings()
    end = ensure_utc(as_of) if as_of is not None else utc_now()
    start = end - timedelta(days=days)
    out_dir = reports_dir if reports_dir is not None else REPORTS_DIR

    if session is not None:
        cohorts = collect_day_cohorts(session, start=start, end=end)
    else:
        with session_scope() as active:
            cohorts = collect_day_cohorts(active, start=start, end=end)

    report = evaluate_cohorts(cohorts, settings=settings, window_start=start, window_end=end)

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = end.strftime("%Y%m%d")
    json_path = out_dir / f"entry-rank-falsification-{stamp}.json"
    md_path = out_dir / f"entry-rank-falsification-{stamp}.md"
    json_path.write_text(json.dumps(_report_payload(report), indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    lift_text = f"{report.lift:.3f}" if report.lift is not None else "n/a"
    print(
        f"entry-rank falsification: composite={report.composite_precision:.4f} "
        f"volume={report.volume_precision:.4f} lift={lift_text} "
        f"(kill bar >= {KILL_LIFT:g}x) -> {'PASS' if report.passed else 'FAIL'}; "
        f"wrote {json_path} + {md_path}"
    )
    return 0 if report.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Entry-rank falsification run (Task 5)")
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 timestamp ending the evaluation window (default: now, UTC)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="evaluation window length in days (default: 14)",
    )
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    return run_falsification(as_of=as_of, days=args.days)


if __name__ == "__main__":
    sys.exit(main())
