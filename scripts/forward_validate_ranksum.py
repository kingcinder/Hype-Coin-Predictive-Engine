#!/usr/bin/env python3
"""Forward-validate the rank-sum candidate rule (Ticket 1) on fresh data.

Rule (reports/ticket1-edge-hunt-20261120.md section 6):
  For each live-phase row, the rank-sum is the MEAN of the percentile ranks
  (0-1) of `unique_buyers_estimate`, `buy_sell_ratio`, `liquidity_depth`
  among rows where each feature is present; missing -> 0.5 (neutral).
  Predict collapse (BLACK) when the rank-sum is in the top decile AND the
  phase is live (seeding/ignition/parabolic/saturation).

Forward validation (report section 7, mandatory before wiring in):
  * The threshold is FIXED from reference/train data -- the 90th percentile
    of the rank-sum among pre-cutoff (v6) live rows -- never from the eval
    window and never from the fresh window.
  * Ranks for fresh rows are computed against the STORED reference
    distribution (percentiles from the reference window), not the fresh
    batch, so the top decile is not batch-dependent.
  * The gate is passed only if the harness verdict() reads `beats_baseline`
    on the fresh window (asset-level bootstrap CI lower bound above both the
    stratum base rate and its CI upper bound, >= 30 assets).

This script is READ-ONLY with respect to the live DB: it only SELECTs
(outcomes via the repo ORM, features via sqlite3 mode=ro). It never writes
to the DB, never commits, and never touches the running engine.

Usage:
  # Sanity check: reproduce the eval-window result from the saved artifacts
  .venv/bin/python scripts/forward_validate_ranksum.py --reproduce-eval

  # Forward validation on the default fresh window (post-Nov-20).
  # The fixed threshold is loaded from reports/ranksum-reference-20261121.json
  # by default (never recomputed from the live DB, whose reference coverage
  # was destroyed by the Nov 22 prune); use --recompute-reference to rebuild it.
  .venv/bin/python scripts/forward_validate_ranksum.py

  # Forward validation on an explicit window (inclusive start, exclusive end)
  .venv/bin/python scripts/forward_validate_ranksum.py --start 2026-11-18 --end 2026-11-19

  # Also write the fixed threshold + reference distributions to a JSON file
  .venv/bin/python scripts/forward_validate_ranksum.py --save-reference <path>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


# v6 validation cutoff (verification round, fixed). The reference set is the
# pre-cutoff outcomes; this boundary is HARDCODED so the threshold never
# shifts as new outcomes accrue. Do not recompute it from the live data.
V6_CUTOFF = datetime(2026, 10, 23, 3, 11, 42, 918632, tzinfo=UTC)
# Default fresh window: post-Nov-20 (the rule was derived Nov 20 from the
# Nov 14 + Nov 17 eval window, so post-Nov-20 data is unseen by the rule).
FRESH_START = datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC)
FEATURES = ["unique_buyers_estimate", "buy_sell_ratio", "liquidity_depth"]
DEFAULT_DB = "/media/cody/Data/DOCUMENTS_PROJECT_FILES/Hype-Coin-Predictive-Engine-main/serpent.db"
DEFAULT_REF_PATH = REPO / "reports" / "ranksum-reference-20261121.json"

# Repo modules are imported lazily in main() AFTER SERPENT_DB_PATH is set,
# so the settings resolve to the requested DB. They are module globals.
H = None
bootstrap_ci = None
verdict = None
base_rate_proportion = None
base_rate_wilson_ci = None
rankdata = None
roc_auc_score = None


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


def load_feature_matrix(db_path: str, keys: list) -> dict:
    """Return {(asset_id, decision_ts_str): {feature_name: value}} for keys.

    Read-only (sqlite3 mode=ro). Mirrors step3's TEMP-table join: only
    rows with missing_flag=0 are used; absent features stay missing.
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
            "SELECT f.asset_id, f.decision_ts, f.feature_name, f.feature_value "
            "FROM features f JOIN keys k "
            "ON f.asset_id=k.asset_id AND f.decision_ts=k.decision_ts "
            "WHERE f.missing_flag=0"
        )
        for aid, dts, name, val in con.execute(q):
            out.setdefault((int(aid), dts), {})[name] = float(val)
        return out
    finally:
        con.close()


def pct_rank(value: float, ref_sorted: np.ndarray) -> float:
    """Percentile rank (0-1) of value against a stored reference distribution.

    Midrank definition (matches rankdata's tie-averaging): the fraction of
    reference values below, plus half the fraction equal. Missing -> 0.5.
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0.5
    n = len(ref_sorted)
    if n == 0:
        return 0.5
    lo = int(np.searchsorted(ref_sorted, value, side="left"))
    hi = int(np.searchsorted(ref_sorted, value, side="right"))
    return (lo + 0.5 * (hi - lo)) / n


def rank_sum_for_row(feat: dict, ref_dists: dict) -> float:
    """Mean of the three percentile ranks (missing -> 0.5)."""
    return float(np.mean([pct_rank(feat.get(f), ref_dists[f]) for f in FEATURES]))


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


def build_reference(db_path: str, live_outcomes: list) -> dict:
    """Reference distributions + fixed threshold from pre-cutoff live rows."""
    ref = [o for o in live_outcomes if o.decision_ts <= V6_CUTOFF]
    print(f"reference live rows (decision_ts <= v6 cutoff): n={len(ref)}", flush=True)
    if not ref:
        raise SystemExit("no pre-cutoff live rows; cannot fix the threshold")
    keys = [(o.asset_id, o.decision_ts) for o in ref]
    feat = load_feature_matrix(db_path, keys)
    for f in FEATURES:
        have = sum(1 for k in keys if f in feat.get((k[0], norm_ts(k[1])), {}))
        print(f"  reference coverage {f}: {have}/{len(keys)}", flush=True)
    ref_dists: dict[str, np.ndarray] = {}
    for f in FEATURES:
        vals = sorted(
            feat[(a, norm_ts(ts))][f] for a, ts in keys if f in feat.get((a, norm_ts(ts)), {})
        )
        ref_dists[f] = np.array(vals, dtype=float)
        print(f"  reference distribution {f}: n={len(vals)}", flush=True)
    ref_scores = np.array(
        [
            rank_sum_for_row(feat.get((o.asset_id, norm_ts(o.decision_ts)), {}), ref_dists)
            for o in ref
        ]
    )
    threshold = float(np.percentile(ref_scores, 90))
    print(
        f"reference rank-sum: n={len(ref_scores)} min={ref_scores.min():.4f} "
        f"median={np.median(ref_scores):.4f} p90={threshold:.4f} max={ref_scores.max():.4f}",
        flush=True,
    )
    print(
        f"FIXED threshold (90th percentile of reference live rank-sum): {threshold:.6f}",
        flush=True,
    )
    return {
        "threshold": threshold,
        "ref_dists": ref_dists,
        "ref_n": len(ref),
        "ref_scores": ref_scores,
    }


def load_reference(path: str) -> dict:
    """Load a fixed threshold + reference distributions from a saved JSON.

    The JSON is written by --save-reference (threshold + per-feature sorted
    reference values).  Loading it -- instead of recomputing from the live
    DB -- is what keeps the threshold fixed when the live DB has lost
    reference coverage (e.g. the Nov 22 prune cut it 118 -> 4, which
    recomputed a degenerate 0.500000 threshold).
    """
    payload = json.loads(Path(path).read_text())
    ref_dists = {
        f: np.array(sorted(float(v) for v in payload["ref_distributions"][f]), dtype=float)
        for f in FEATURES
    }
    return {
        "threshold": float(payload["threshold"]),
        "ref_dists": ref_dists,
        "ref_n": int(payload["ref_n"]),
    }


def validate_window(
    name: str,
    live_outcomes: list,
    ref_dists: dict,
    threshold: float,
    db_path: str,
    start: datetime,
    end: datetime | None,
) -> dict:
    """Apply the FIXED threshold to one window and measure it."""
    rows = [
        o for o in live_outcomes if o.decision_ts > start and (end is None or o.decision_ts <= end)
    ]
    print(
        f"\n[{name}] window: decision_ts > {start.isoformat()}"
        + (f" and <= {end.isoformat()}" if end else ""),
        flush=True,
    )
    print(f"[{name}] live outcomes in window: n={len(rows)}", flush=True)
    if not rows:
        print(f"[{name}] no rows; validation cannot run on this window.", flush=True)
        return {"name": name, "n": 0, "verdict": "insufficient_data"}
    keys = [(o.asset_id, o.decision_ts) for o in rows]
    feat = load_feature_matrix(db_path, keys)
    for f in FEATURES:
        have = sum(1 for k in keys if f in feat.get((k[0], norm_ts(k[1])), {}))
        print(f"  [{name}] coverage {f}: {have}/{len(keys)}", flush=True)
    labels = np.array([lab_row(o.collapsed, o.rugged) for o in rows], dtype=float)
    assets = np.array([o.asset_id for o in rows], dtype=int)
    scores = np.array(
        [
            rank_sum_for_row(feat.get((o.asset_id, norm_ts(o.decision_ts)), {}), ref_dists)
            for o in rows
        ]
    )
    flag = scores >= threshold
    result = measure(f"[{name}] rank-sum >= fixed threshold", labels, assets, flag)
    result["threshold"] = threshold
    days: dict[str, list] = {}
    for o, s, lab in zip(rows, scores, labels, strict=True):
        days.setdefault(o.decision_ts.strftime("%Y-%m-%d"), []).append(
            (bool(s >= threshold), float(lab))
        )
    for d in sorted(days):
        sel = [lab for f_, lab in days[d] if f_]
        all_ = [lab for _, lab in days[d]]
        print(
            f"  [{name}] day {d}: n={len(all_)} base={np.mean(all_):.4f} "
            f"selected_n={len(sel)} precision={np.mean(sel) if sel else float('nan'):.4f}",
            flush=True,
        )
    return result


def reproduce_eval() -> None:
    """Reproduce the eval-window result from the saved /tmp/ticket1 artifacts.

    Verifies the measurement code (rank-sum, bootstrap CI, verdict) by
    replaying step4/step5 exactly: ranks within the eval population, top 497.
    Expect precision 0.1268, CI [0.0364, 0.2567], verdict beats_baseline.
    """
    print("=== reproduce-eval sanity check (saved /tmp/ticket1 artifacts) ===", flush=True)
    live_rows = json.load(open("/tmp/ticket1/live_rows.json"))
    X = np.load("/tmp/ticket1/live_feature_matrix.npy")
    feat_names = json.load(open("/tmp/ticket1/live_feature_names.json"))
    fidx = {n: j for j, n in enumerate(feat_names)}
    labels = np.array([lab_row(r["collapsed"], r["rugged"]) for r in live_rows], dtype=float)
    assets = np.array([r["asset_id"] for r in live_rows], dtype=int)

    def ranks_present(col: np.ndarray) -> np.ndarray:
        m = ~np.isnan(col)
        r = np.full(len(col), 0.5)
        r[m] = rankdata(col[m]) / m.sum()
        return r

    combo = np.mean(
        [ranks_present(X[:, fidx[n]]) for n in FEATURES],
        axis=0,
    )
    print(f"combo AUC recheck: {roc_auc_score(labels, combo):.4f} (expect 0.7742)", flush=True)
    order = np.argsort(-combo, kind="stable")
    top = np.zeros(len(combo), dtype=bool)
    top[order[:497]] = True
    result = measure("reproduce-eval COMBO top-10% (top 497)", labels, assets, top)
    ok = abs(result["precision"] - 0.1268) < 0.001 and result["verdict"] == "beats_baseline"
    print(
        f"reproduce-eval: {'PASS' if ok else 'FAIL'} "
        "(expect precision 0.1268, verdict beats_baseline)",
        flush=True,
    )
    if not ok:
        raise SystemExit("reproduce-eval FAILED; measurement code does not match the edge hunt")


def reference_auc_check(db_path: str, live_outcomes: list) -> None:
    """Re-derive the feature selection from reference rows only (report s7).

    The top-3 were selected by AUC on the eval window, so confirm the same
    trio emerges from pre-cutoff (reference) rows alone. Diagnostic only;
    it does not gate the forward validation.
    """
    ref = [o for o in live_outcomes if o.decision_ts <= V6_CUTOFF]
    if len(ref) < 30:
        print("reference AUC check: too few reference live rows, skipped", flush=True)
        return
    keys = [(o.asset_id, o.decision_ts) for o in ref]
    feat = load_feature_matrix(db_path, keys)
    labels = np.array([lab_row(o.collapsed, o.rugged) for o in ref], dtype=float)
    print(
        f"reference AUC check: n={len(ref)} collapses={int(labels.sum())} base={labels.mean():.4f}",
        flush=True,
    )
    scored = []
    for f in FEATURES + ["spread_estimate", "volume_acceleration", "pair_age_minutes"]:
        col = np.array(
            [feat.get((o.asset_id, norm_ts(o.decision_ts)), {}).get(f, np.nan) for o in ref],
            dtype=float,
        )
        m = ~np.isnan(col)
        if m.sum() < 30 or labels[m].sum() == 0 or labels[m].sum() == m.sum():
            continue
        try:
            auc = roc_auc_score(labels[m], col[m])
        except Exception:
            continue
        scored.append((f, auc, int(m.sum())))
    for f, auc, n in sorted(scored, key=lambda t: -abs(t[1] - 0.5)):
        mark = " <-- candidate trio" if f in FEATURES else ""
        print(f"  reference AUC {f}: {auc:.4f} (n={n}){mark}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Forward-validate the rank-sum candidate rule.")
    ap.add_argument("--db-path", default=None, help="SQLite DB path (default: live serpent.db)")
    ap.add_argument(
        "--start", default=None, help="window start ISO date/datetime (default: 2026-11-20)"
    )
    ap.add_argument("--end", default=None, help="window end ISO date/datetime (default: none)")
    ap.add_argument(
        "--reproduce-eval", action="store_true", help="replay the eval-window result and exit"
    )
    ap.add_argument(
        "--save-reference", default=None, help="write fixed threshold + reference dists to JSON"
    )
    ap.add_argument(
        "--load-reference",
        default=None,
        help=(
            "load fixed threshold + reference dists from JSON "
            "(default: reports/ranksum-reference-20261121.json when it exists)"
        ),
    )
    ap.add_argument(
        "--recompute-reference",
        action="store_true",
        help="recompute the reference from the live DB instead of loading the saved JSON",
    )
    ap.add_argument(
        "--skip-reference-auc", action="store_true", help="skip the reference AUC diagnostic"
    )
    args = ap.parse_args()

    global H, bootstrap_ci, verdict, base_rate_proportion, base_rate_wilson_ci, rankdata
    global roc_auc_score

    db_path = args.db_path or os.environ.get("SERPENT_DB_PATH", DEFAULT_DB)
    # Set the DB path BEFORE importing repo modules so settings resolve to it.
    os.environ["SERPENT_DB_PATH"] = db_path

    from scipy.stats import rankdata as _rankdata  # noqa: E402
    from sklearn.metrics import roc_auc_score as _roc  # noqa: E402

    from validation import harness as _H  # noqa: E402
    from validation.baselines import (  # noqa: E402
        base_rate_proportion as _brp,
    )
    from validation.baselines import (
        base_rate_wilson_ci as _brw,
    )
    from validation.metrics import bootstrap_ci as _bc  # noqa: E402
    from validation.metrics import verdict as _verdict

    H = _H
    bootstrap_ci = _bc
    verdict = _verdict
    base_rate_proportion = _brp
    base_rate_wilson_ci = _brw
    rankdata = _rankdata
    roc_auc_score = _roc

    if args.reproduce_eval:
        reproduce_eval()
        return

    print(f"DB (read-only): {db_path}", flush=True)
    live = load_live_outcomes(db_path)
    print(f"live outcomes loaded: n={len(live)}", flush=True)

    if args.recompute_reference:
        ref = build_reference(db_path, live)
        threshold = ref["threshold"]
        ref_dists = ref["ref_dists"]
    else:
        ref_path = args.load_reference or str(DEFAULT_REF_PATH)
        if os.path.exists(ref_path):
            ref = load_reference(ref_path)
            threshold = ref["threshold"]
            ref_dists = ref["ref_dists"]
            print(f"loaded fixed threshold + reference distributions from {ref_path}", flush=True)
            print(
                f"FIXED threshold (loaded): {threshold:.6f} (ref_n={ref['ref_n']})",
                flush=True,
            )
        else:
            print(
                f"no saved reference at {ref_path}; recomputing from the live DB "
                "(coverage may be thin after the Nov 22 prune)",
                flush=True,
            )
            ref = build_reference(db_path, live)
            threshold = ref["threshold"]
            ref_dists = ref["ref_dists"]

    if args.save_reference:
        payload = {
            "v6_cutoff": V6_CUTOFF.isoformat(),
            "features": FEATURES,
            "threshold": threshold,
            "ref_n": ref["ref_n"],
            "ref_distributions": {f: sorted(float(v) for v in ref_dists[f]) for f in FEATURES},
        }
        Path(args.save_reference).write_text(json.dumps(payload, indent=2))
        print(
            f"saved fixed threshold + reference distributions to {args.save_reference}",
            flush=True,
        )

    if not args.skip_reference_auc:
        reference_auc_check(db_path, live)

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start else FRESH_START
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else None
    validate_window("fresh", live, ref_dists, threshold, db_path, start, end)


if __name__ == "__main__":
    main()
