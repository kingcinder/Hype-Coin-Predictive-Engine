#!/usr/bin/env python3
"""Forward-validate the pair-age bottom-decile candidate rule (Ticket 1).

Rule (ticket1-attack-report-2026-11-22): the youngest 10% of pairs
(bottom decile of pair_age) collapse at a much higher rate than baseline.
Predict collapse when pair_age_minutes is in the bottom decile AND the
phase is live (seeding/ignition/parabolic/saturation).

The threshold is FIXED from reference data -- the 10th percentile of
pair_age_minutes among pre-Nov-20 reference rows (decision_ts < 2026-11-20,
joined to risk_outcomes): 555.27 minutes (~9.25 h), n=836.  The reference
is thin because the Nov 22 prune destroyed most pre-Nov-20 feature
coverage (the pre-Oct-23 V6 population has only n=13 pair_age rows and is
unusable).  The threshold is never recomputed from the fresh window.

Forward validation (same gate as the rank-sum rule): the harness verdict()
must read `beats_baseline` on the fresh window (asset-level bootstrap CI
lower bound above both the stratum base rate and its CI upper bound,
>= 30 assets) before the rule is wired in.

This script is READ-ONLY with respect to the live DB: it only SELECTs
(outcomes via the repo ORM, features via sqlite3 mode=ro). It never writes
to the DB, never commits, and never touches the running engine.

Usage:
  # Forward validation on the default fresh window (post-Nov-20)
  .venv/bin/python scripts/forward_validate_pair_age.py

  # Override the fixed threshold (minutes)
  .venv/bin/python scripts/forward_validate_pair_age.py --threshold 555.27
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


# Default fresh window: post-Nov-20 (the rule was derived Nov 20-22, so
# post-Nov-20 data is unseen by the rule).
FRESH_START = datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC)
# Fixed threshold: 10th percentile of pair_age_minutes on pre-Nov-20
# reference rows (n=836, post-prune thin sample). Youngest 10% of pairs.
PAIR_AGE_P10_MINUTES = 555.27
DEFAULT_DB = "/media/cody/Data/DOCUMENTS_PROJECT_FILES/Hype-Coin-Predictive-Engine-main/serpent.db"

# Repo modules are imported lazily in main() AFTER SERPENT_DB_PATH is set,
# so the settings resolve to the requested DB. They are module globals.
H = None
bootstrap_ci = None
verdict = None
base_rate_proportion = None
base_rate_wilson_ci = None


def lab_row(collapsed: bool, rugged: bool) -> float:
    return 1.0 if (collapsed or rugged) else 0.0


def norm_ts(dt: datetime) -> str:
    """Format a decision_ts exactly as step3 did for the features join."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def load_live_outcomes(db_path: str) -> list:
    """Load all evaluated v3 outcomes, live-phase only, via the repo harness."""
    from storage.database import session_scope

    with session_scope() as session:
        outcomes = H.load_outcomes(session)
    live = [o for o in outcomes if (o.phase_at_score or "") in H.LIVE_PHASES]
    return live


def load_pair_age(db_path: str, keys: list) -> dict:
    """Return {(asset_id, decision_ts_str): pair_age_minutes} for keys.

    Read-only (sqlite3 mode=ro). Only rows with missing_flag=0 are used.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.execute("CREATE TEMP TABLE keys(asset_id INTEGER, decision_ts TEXT)")
        con.executemany(
            "INSERT INTO keys VALUES (?, ?)",
            [(int(a), norm_ts(ts)) for a, ts in keys],
        )
        con.commit()
        out: dict = {}
        q = (
            "SELECT f.asset_id, f.decision_ts, f.feature_value "
            "FROM features f JOIN keys k "
            "ON f.asset_id=k.asset_id AND f.decision_ts=k.decision_ts "
            "WHERE f.missing_flag=0 AND f.feature_name='pair_age_minutes'"
        )
        for aid, dts, val in con.execute(q):
            out[(int(aid), dts)] = float(val)
        return out
    finally:
        con.close()


def measure(name: str, labels: np.ndarray, assets: np.ndarray, flag: np.ndarray) -> dict:
    """Precision / base rate / asset-bootstrap CI / verdict for a selection."""
    sel_lab = labels[flag]
    sel_assets = assets[flag]
    prec = float(sel_lab.mean()) if len(sel_lab) else float("nan")
    groups: dict[int, list[float]] = {}
    for a, lab in zip(sel_assets, sel_lab, strict=True):
        groups.setdefault(int(a), []).append(float(lab))
    gl = list(groups.values())
    ci = (
        bootstrap_ci(gl, lambda x: float(np.mean(x)))
        if len(gl) > 1
        else (float("nan"), float("nan"))
    )
    base = base_rate_proportion(labels)
    bci = base_rate_wilson_ci(labels)
    ver = verdict(prec, ci[0], ci[1], base, bci.low, bci.high, len(gl), False, min_assets=30)
    result = {
        "name": name,
        "n": int(len(labels)),
        "n_assets": int(len(set(int(a) for a in assets))),
        "collapses": int(labels.sum()),
        "base_rate": float(base),
        "base_ci": [float(bci.low), float(bci.high)],
        "selected_n": int(flag.sum()),
        "selected_assets": int(len(gl)),
        "selected_collapses": int(sel_lab.sum()),
        "precision": prec,
        "ci": [float(ci[0]), float(ci[1])],
        "verdict": ver,
    }
    print(
        f"{name}: n={result['n']} n_assets={result['n_assets']} collapses={result['collapses']} "
        f"base={result['base_rate']:.4f} "
        f"base_ci=[{result['base_ci'][0]:.4f},{result['base_ci'][1]:.4f}] "
        f"selected_n={result['selected_n']} selected_assets={result['selected_assets']} "
        f"selected_collapses={result['selected_collapses']} precision={result['precision']:.4f} "
        f"ci=[{result['ci'][0]:.4f},{result['ci'][1]:.4f}] verdict={result['verdict']}",
        flush=True,
    )
    return result


def main() -> None:
    global H, bootstrap_ci, verdict, base_rate_proportion, base_rate_wilson_ci
    ap = argparse.ArgumentParser(description="Forward-validate the pair-age bottom-decile rule.")
    ap.add_argument("--db", default=None, help="path to serpent.db (default: repo serpent.db)")
    ap.add_argument(
        "--start", default=None, help="window start ISO date/datetime (default: 2026-11-20)"
    )
    ap.add_argument("--end", default=None, help="window end ISO date/datetime (exclusive)")
    ap.add_argument(
        "--threshold",
        type=float,
        default=PAIR_AGE_P10_MINUTES,
        help=f"pair_age_minutes bottom-decile threshold (default: {PAIR_AGE_P10_MINUTES})",
    )
    args = ap.parse_args()

    db_path = args.db or os.environ.get("SERPENT_DB_PATH") or DEFAULT_DB
    if args.db:
        os.environ["SERPENT_DB_PATH"] = args.db

    from validation import harness as _H
    from validation.harness import (
        base_rate_proportion as _brp,
    )
    from validation.harness import (
        base_rate_wilson_ci as _brw,
    )
    from validation.harness import (
        bootstrap_ci as _bci,
    )
    from validation.harness import (
        verdict as _verdict,
    )

    H = _H
    bootstrap_ci = _bci
    verdict = _verdict
    base_rate_proportion = _brp
    base_rate_wilson_ci = _brw

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start else FRESH_START
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else None

    print(f"DB (read-only): {db_path}", flush=True)
    live = load_live_outcomes(db_path)
    print(f"live outcomes loaded: n={len(live)}", flush=True)
    print(
        f"FIXED pair-age threshold: {args.threshold:.2f} minutes "
        f"(10th pct of pair_age_minutes on pre-Nov-20 reference rows, n=836)",
        flush=True,
    )

    fresh = [o for o in live if o.decision_ts > start and (end is None or o.decision_ts < end)]
    print(f"\n[fresh] window: decision_ts > {start.isoformat()}", flush=True)
    print(f"[fresh] live outcomes in window: n={len(fresh)}", flush=True)
    if not fresh:
        print("no fresh live outcomes; nothing to validate", flush=True)
        return

    keys = [(o.asset_id, o.decision_ts) for o in fresh]
    pair_age = load_pair_age(db_path, keys)
    print(f"[fresh] pair_age_minutes coverage: {len(pair_age)}/{len(fresh)}", flush=True)

    labels = np.array([lab_row(o.collapsed, o.rugged) for o in fresh], dtype=float)
    assets = np.array([o.asset_id for o in fresh], dtype=int)
    # Bottom decile: youngest pairs (pair_age <= threshold). Missing -> not selected.
    flag = np.array(
        [
            (pair_age.get((o.asset_id, norm_ts(o.decision_ts))) is not None)
            and pair_age[(o.asset_id, norm_ts(o.decision_ts))] <= args.threshold
            for o in fresh
        ],
        dtype=bool,
    )
    measure("[fresh] pair_age bottom-decile", labels, assets, flag)


if __name__ == "__main__":
    main()
