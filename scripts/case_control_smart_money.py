#!/usr/bin/env python3
"""Smart-money convergence case-control gate — the Task 12a artifact.

Spec §3 item 5 asks whether point-in-time profitable-wallet
convergence predicts pumps, and forbids building the wallet-PnL table
(12b) until a case-control test clears >= 3x. The lake persists no
per-wallet trade/PnL history (``models.Holder`` is snapshots only), so
the gate runs on the proxy the data *does* support:

- A **decision point** is an asset at a ``forward_max_gain_pct`` label
  timestamp (Task 2's continuous labels); a point is a **case** when
  its forward max gain reaches >= 1.0 (a doubling in-window).
- A wallet is a **repeat winner** at a decision when it was an early
  holder — present in ``holders`` within the 6h before the decision,
  on rows observed by the decision — of >= 2 *prior* cases (case
  decisions strictly earlier than this one).
- A point is **converged** when >= ``CONVERGENCE_MIN_WALLETS`` of its
  own early holders are repeat winners at its decision.

The winner set is built strictly from data observed before each
decision: holder rows carry the lake's causality discipline
(``ts <= decision`` and ``observed_at <= decision``, the same rule
``backtest.runner.point_in_time_market_rows`` enforces), and a case
only becomes winner evidence for decisions after its own — a wallet
whose first case-win lands after a decision point does not count for
that point.

The gate compares pump shares: converged points vs **matched
controls** — non-converged points from the same day and the same
``liquidity_depth`` decile (deciles computed within the day over
depth-known points; equal depths share a decile; points with no
point-in-time depth print form their own stratum). Exit code: 0 iff
the converged pump share is >= 3x the control share with n >= 30
converged points, else 1. The report (JSON + MD) is written either
way. The report also records the spec's case-side formulation —
convergence share among cases vs among matched non-case controls —
for the record; the gate verdict is the plan's pump-share criterion.

Only an exit-0 verdict on a real run authorizes 12b (the point-in-time
wallet-PnL table and the ``smart_money_convergence`` feature). A fail
cancels 12b; no verdict at all (this script never run against a real
DB) leaves 12b unbuilt and the gate UNMEASURED — see STATE.md.

Usage:
  python scripts/case_control_smart_money.py [--as-of <ISO-8601>]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from common.time import ensure_utc, utc_now  # noqa: E402
from forecast.labels import LABEL_FORWARD_MAX_GAIN  # noqa: E402
from storage import models  # noqa: E402
from storage.database import session_scope  # noqa: E402

REPORTS_DIR = REPO / "reports"

# The gate bars (plan Task 12a / spec §3 item 5). These are the
# research question, not policy knobs.
PUMP_GAIN_THRESHOLD = 1.0  # a case: forward_max_gain_pct >= 1.0 (>= 2x)
HOLDER_WINDOW_HOURS = 6  # "early holder" / convergence window pre-decision
REPEAT_WINNER_MIN_CASES = 2  # early holder of >= 2 prior cases
CONVERGENCE_MIN_WALLETS = 3  # >= 3 repeat-winner holders (spec item 5's >= 3)
GATE_MIN_RATIO = 3.0  # converged pump share >= 3x control share
GATE_MIN_CONVERGED_N = 30  # ... on at least 30 converged points


@dataclass(frozen=True)
class PointEvaluation:
    asset_id: int
    ts: datetime
    forward_max_gain_pct: float
    pumped: bool
    liquidity_depth: float | None
    liquidity_decile: int | None
    repeat_winner_holders: int
    converged: bool


@dataclass(frozen=True)
class CaseControlStats:
    as_of: datetime
    points: int
    cases: int
    converged_points: int
    converged_pumped: int
    converged_pump_share: float | None
    control_points: int
    control_pumped: int
    control_pump_share: float | None
    pump_share_ratio: float | None
    case_convergence_share: float | None
    matched_control_convergence_share: float | None
    passed: bool


def _decision_points(session: Session, *, as_of: datetime) -> list[tuple[int, datetime, float]]:
    """All forward-max-gain label points observed by ``as_of``,
    sorted by (ts, asset_id) for deterministic evaluation."""
    rows = session.execute(
        select(models.Label.asset_id, models.Label.ts, models.Label.label_value).where(
            models.Label.label_type == LABEL_FORWARD_MAX_GAIN,
            models.Label.ts <= as_of,
            models.Label.observed_at <= as_of,
        )
    ).all()
    points: list[tuple[int, datetime, float]] = []
    for asset_id, ts, raw_value in rows:
        try:
            gain = float(raw_value)
        except (TypeError, ValueError):
            continue
        points.append((int(asset_id), ensure_utc(ts), gain))
    points.sort(key=lambda point: (point[1], point[0]))
    return points


def _liquidity_depths(
    session: Session, points: list[tuple[int, datetime, float]]
) -> dict[tuple[int, datetime], float | None]:
    """Point-in-time ``liquidity_depth`` per point: the reserve_usd of
    the asset's latest liquidity snapshot with ts and observed_at at
    or before the decision (the feature factory's own definition)."""
    rows_by_asset: dict[int, list[tuple[datetime, datetime, float | None]]] = {}
    for asset_id in {point[0] for point in points}:
        pool_ids = list(
            session.scalars(
                select(models.Pair.pool_id).where(models.Pair.base_asset_id == asset_id).distinct()
            )
        )
        rows: list[tuple[datetime, datetime, float | None]] = []
        if pool_ids:
            rows = [
                (ensure_utc(row.ts), ensure_utc(row.observed_at), row.reserve_usd)
                for row in session.scalars(
                    select(models.LiquiditySnapshot)
                    .where(models.LiquiditySnapshot.pool_id.in_(pool_ids))
                    .order_by(models.LiquiditySnapshot.ts, models.LiquiditySnapshot.id)
                )
            ]
        rows_by_asset[asset_id] = rows
    depths: dict[tuple[int, datetime], float | None] = {}
    for asset_id, ts, _gain in points:
        depth: float | None = None
        for row_ts, row_observed_at, reserve_usd in rows_by_asset[asset_id]:
            if row_ts > ts:
                break
            if row_observed_at <= ts:
                depth = reserve_usd
        depths[(asset_id, ts)] = None if depth is None else float(depth)
    return depths


def _deciles(
    points: list[tuple[int, datetime, float]],
    depths: dict[tuple[int, datetime], float | None],
) -> dict[tuple[int, datetime], int]:
    """Within-day liquidity-depth deciles. A point's decile counts the
    share of the day's depth-known points strictly below it, so equal
    depths share a decile; depth-missing points get no decile and form
    their own stratum."""
    by_day: dict[date, list[tuple[int, datetime, float]]] = {}
    for asset_id, ts, _gain in points:
        depth = depths[(asset_id, ts)]
        if depth is not None:
            by_day.setdefault(ts.date(), []).append((asset_id, ts, depth))
    deciles: dict[tuple[int, datetime], int] = {}
    for day_points in by_day.values():
        total = len(day_points)
        for asset_id, ts, depth in day_points:
            below = sum(1 for _a, _t, other in day_points if other < depth)
            deciles[(asset_id, ts)] = min(9, (10 * below) // total)
    return deciles


def _holder_rows(
    session: Session, asset_ids: set[int]
) -> dict[int, list[tuple[str, datetime, datetime]]]:
    return {
        asset_id: [
            (row.wallet_address, ensure_utc(row.ts), ensure_utc(row.observed_at))
            for row in session.scalars(
                select(models.Holder)
                .where(models.Holder.asset_id == asset_id)
                .order_by(models.Holder.ts, models.Holder.id)
            )
        ]
        for asset_id in asset_ids
    }


def _early_holders(
    rows: list[tuple[str, datetime, datetime]], *, decision_ts: datetime
) -> set[str]:
    """Wallets holding within the 6h before ``decision_ts`` on rows
    observed by the decision — the lake's causality discipline."""
    window_start = decision_ts - timedelta(hours=HOLDER_WINDOW_HOURS)
    return {
        wallet
        for wallet, ts, observed_at in rows
        if window_start <= ts <= decision_ts and observed_at <= decision_ts
    }


def evaluate_points(
    session: Session,
    *,
    as_of: datetime,
    min_wallets: int = CONVERGENCE_MIN_WALLETS,
) -> list[PointEvaluation]:
    """Judge every decision point observed by ``as_of``, point-in-time.

    Points are processed in decision order, grouped by decision
    timestamp: a group is judged against the repeat-winner set built
    from cases with strictly earlier decisions only, and the group's
    own cases join the evidence afterwards — same-timestamp points
    never certify each other.
    """
    as_of = ensure_utc(as_of)
    points = _decision_points(session, as_of=as_of)
    depths = _liquidity_depths(session, points)
    deciles = _deciles(points, depths)
    holders = _holder_rows(session, {point[0] for point in points})

    case_wins: dict[str, set[tuple[int, datetime]]] = {}
    evaluations: list[PointEvaluation] = []
    index = 0
    while index < len(points):
        ts = points[index][1]
        group: list[tuple[int, datetime, float]] = []
        while index < len(points) and points[index][1] == ts:
            group.append(points[index])
            index += 1
        winners = {
            wallet for wallet, wins in case_wins.items() if len(wins) >= REPEAT_WINNER_MIN_CASES
        }
        judged: list[tuple[int, datetime, float, set[str], int]] = []
        for asset_id, point_ts, gain in group:
            early = _early_holders(holders[asset_id], decision_ts=point_ts)
            repeat = len(early & winners)
            judged.append((asset_id, point_ts, gain, early, repeat))
            evaluations.append(
                PointEvaluation(
                    asset_id=asset_id,
                    ts=point_ts,
                    forward_max_gain_pct=gain,
                    pumped=gain >= PUMP_GAIN_THRESHOLD,
                    liquidity_depth=depths[(asset_id, point_ts)],
                    liquidity_decile=deciles.get((asset_id, point_ts)),
                    repeat_winner_holders=repeat,
                    converged=repeat >= min_wallets,
                )
            )
        for asset_id, point_ts, gain, early, _repeat in judged:
            if gain >= PUMP_GAIN_THRESHOLD:
                for wallet in early:
                    case_wins.setdefault(wallet, set()).add((asset_id, point_ts))
    return evaluations


def _share(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def convergence_stats(
    session: Session,
    *,
    as_of: datetime,
    min_wallets: int = CONVERGENCE_MIN_WALLETS,
) -> CaseControlStats:
    """The gate statistic: pump share among converged points vs pump
    share among matched controls (non-converged points sharing a
    converged point's day + liquidity-decile stratum)."""
    evaluations = evaluate_points(session, as_of=as_of, min_wallets=min_wallets)
    converged = [ev for ev in evaluations if ev.converged]
    cases = [ev for ev in evaluations if ev.pumped]
    converged_strata = {(ev.ts.date(), ev.liquidity_decile) for ev in converged}
    controls = [
        ev
        for ev in evaluations
        if not ev.converged and (ev.ts.date(), ev.liquidity_decile) in converged_strata
    ]
    # The plan's case-control pairing, recorded for the spec's
    # formulation: controls matched to cases (same day + decile) below
    # the ignition threshold — non-cases by construction.
    case_strata = {(ev.ts.date(), ev.liquidity_decile) for ev in cases}
    matched_controls = [
        ev
        for ev in evaluations
        if not ev.pumped and (ev.ts.date(), ev.liquidity_decile) in case_strata
    ]
    converged_pumped = sum(1 for ev in converged if ev.pumped)
    control_pumped = sum(1 for ev in controls if ev.pumped)
    converged_share = _share(converged_pumped, len(converged))
    control_share = _share(control_pumped, len(controls))
    ratio = (
        converged_share / control_share if converged_share is not None and control_share else None
    )
    passed = (
        ratio is not None and ratio >= GATE_MIN_RATIO and len(converged) >= GATE_MIN_CONVERGED_N
    )
    return CaseControlStats(
        as_of=ensure_utc(as_of),
        points=len(evaluations),
        cases=len(cases),
        converged_points=len(converged),
        converged_pumped=converged_pumped,
        converged_pump_share=converged_share,
        control_points=len(controls),
        control_pumped=control_pumped,
        control_pump_share=control_share,
        pump_share_ratio=ratio,
        case_convergence_share=_share(sum(1 for ev in cases if ev.converged), len(cases)),
        matched_control_convergence_share=_share(
            sum(1 for ev in matched_controls if ev.converged), len(matched_controls)
        ),
        passed=passed,
    )


def _report_payload(stats: CaseControlStats) -> dict:
    return {
        "as_of": stats.as_of.isoformat(),
        "pump_gain_threshold": PUMP_GAIN_THRESHOLD,
        "holder_window_hours": HOLDER_WINDOW_HOURS,
        "repeat_winner_min_cases": REPEAT_WINNER_MIN_CASES,
        "convergence_min_wallets": CONVERGENCE_MIN_WALLETS,
        "gate_min_ratio": GATE_MIN_RATIO,
        "gate_min_converged_n": GATE_MIN_CONVERGED_N,
        "points": stats.points,
        "cases": stats.cases,
        "converged_points": stats.converged_points,
        "converged_pumped": stats.converged_pumped,
        "converged_pump_share": stats.converged_pump_share,
        "control_points": stats.control_points,
        "control_pumped": stats.control_pumped,
        "control_pump_share": stats.control_pump_share,
        "pump_share_ratio": stats.pump_share_ratio,
        "case_convergence_share": stats.case_convergence_share,
        "matched_control_convergence_share": stats.matched_control_convergence_share,
        "passed": stats.passed,
    }


def _render_markdown(payload: dict) -> str:
    ratio = payload["pump_share_ratio"]
    converged_share = payload["converged_pump_share"]
    control_share = payload["control_pump_share"]
    return "\n".join(
        [
            f"# Smart-money case-control gate — {payload['as_of'][:10]}",
            "",
            f"- as_of: {payload['as_of']}",
            f"- case: forward_max_gain_pct >= {payload['pump_gain_threshold']}",
            f"- convergence: >= {payload['convergence_min_wallets']} repeat-winner "
            f"holders (early holders of >= {payload['repeat_winner_min_cases']} "
            f"prior cases) within {payload['holder_window_hours']}h pre-decision",
            f"- decision points: {payload['points']} (cases: {payload['cases']})",
            f"- converged points: {payload['converged_points']} "
            f"(pumped: {payload['converged_pumped']})",
            f"- matched controls: {payload['control_points']} "
            f"(pumped: {payload['control_pumped']})",
            f"- converged pump share: {converged_share:.4f}"
            if converged_share is not None
            else "- converged pump share: undefined (no converged points)",
            f"- control pump share: {control_share:.4f}"
            if control_share is not None
            else "- control pump share: undefined (no matched controls)",
            f"- pump-share ratio: {ratio:.4f}"
            if ratio is not None
            else "- pump-share ratio: undefined (control share 0)",
            f"- gate: ratio >= {payload['gate_min_ratio']} with "
            f"n(converged) >= {payload['gate_min_converged_n']} -> "
            f"{'PASS' if payload['passed'] else 'FAIL'}",
            "",
            "PASS authorizes the 12b build (point-in-time wallet-PnL table +",
            "smart_money_convergence feature). FAIL cancels 12b (plan Task 12).",
            "",
        ]
    )


def run_case_control(
    *,
    session=None,
    as_of: datetime | None = None,
    reports_dir: Path | None = None,
) -> int:
    """Write the case-control report; return the gate exit code.

    ``session`` is an injectable seam (keyword-only): when None — the
    CLI path — this call opens and owns its own ``session_scope()``
    cycle.
    """
    as_of = ensure_utc(as_of) if as_of is not None else utc_now()
    out_dir = reports_dir if reports_dir is not None else REPORTS_DIR

    if session is not None:
        stats = convergence_stats(session, as_of=as_of)
    else:
        with session_scope() as active:
            stats = convergence_stats(active, as_of=as_of)

    payload = _report_payload(stats)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = as_of.strftime("%Y%m%d")
    json_path = out_dir / f"smart-money-case-control-{stamp}.json"
    md_path = out_dir / f"smart-money-case-control-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    ratio_text = f"{stats.pump_share_ratio:.3f}" if stats.pump_share_ratio is not None else "n/a"
    print(
        f"smart-money gate: converged {stats.converged_pumped}/{stats.converged_points} "
        f"pumped vs controls {stats.control_pumped}/{stats.control_points} "
        f"-> ratio {ratio_text} (bar >= {GATE_MIN_RATIO:g}x, "
        f"n >= {GATE_MIN_CONVERGED_N}) -> {'PASS' if stats.passed else 'FAIL'}; "
        f"wrote {json_path} + {md_path}"
    )
    return 0 if stats.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smart-money convergence case-control gate (Task 12a)"
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 timestamp to measure as of (default: now, UTC)",
    )
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    return run_case_control(as_of=as_of)


if __name__ == "__main__":
    sys.exit(main())
