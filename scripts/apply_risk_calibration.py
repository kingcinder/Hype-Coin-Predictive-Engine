"""Apply the v6 risk-score calibration to the harness eval set on a LAB
database copy — never the live DB.

Usage:
  SERPENT_DB_PATH=<lab copy> .venv/bin/python scripts/apply_risk_calibration.py \
      --cutoff 2026-10-23T03:11:42+00:00 [--expected-eval 27468] [--yes]

Steps:
  1. refresh_phase_confidence(session, cutoff, label_version="v2_bounded")
     — fits the (band, phase) survival map on the reference set (scored_at
     <= cutoff - 48h embargo) and installs it in-process. The risk
     calibration derives from the same map: risk = 100 - confidence.
  2. Load the harness eval set: evaluated v2_bounded outcomes with
     scored_at > cutoff. Assert the count matches --expected-eval when given.
  3. For each eval score, recompute compute_scores from its Feature rows
     and update ONLY `risk` in place. Confidence, uncertainty, band, hype,
     ethos and every other field are left untouched, so the re-run harness
     measures exactly the risk intervention. A confidence mismatch between
     the recomputed value and the stored value is counted (not written) —
     the v6 change must not move confidence.
  4. Commit in batches of 500 and print a before/after summary, including
     mean |risk/100 - collapsed| and corr(uncertainty, risk error).

Default is a dry run (no writes); --yes performs the write pass. The lab DB
is a backup-API copy of the live DB; the live engine keeps running untouched.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import UTC, datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cutoff",
        required=True,
        help="ISO cutoff, e.g. 2026-10-23T03:11:42+00:00 (harness partition)",
    )
    parser.add_argument("--label-version", default="v2_bounded")
    parser.add_argument(
        "--expected-eval", type=int, default=None, help="assert the eval-set row count matches this"
    )
    parser.add_argument(
        "--yes", action="store_true", help="perform the write pass (default is a dry run)"
    )
    parser.add_argument("--db", default=None, help="lab DB path (else SERPENT_DB_PATH env)")
    return parser.parse_args()


def _corr(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def main() -> int:
    args = parse_args()
    cutoff = datetime.fromisoformat(args.cutoff)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    if args.db:
        os.environ["SERPENT_DB_PATH"] = args.db

    from sqlalchemy import select, tuple_

    from common.logging import get_logger
    from scoring.band_calibration import refresh_phase_confidence
    from scoring.formulas import compute_scores
    from storage import models
    from storage.database import session_scope

    log = get_logger(__name__)
    started = time.monotonic()

    with session_scope() as session:
        # 1. Fit the phase map on the reference set and install it in-process.
        summary = refresh_phase_confidence(session, cutoff=cutoff, label_version=args.label_version)
        print("PHASE MAP FIT")
        print(
            json.dumps({k: v for k, v in summary.items() if k != "cells"}, indent=2, sort_keys=True)
        )

        # 2. Load the harness eval set.
        eval_rows = (
            session.query(models.RiskOutcome)
            .filter(
                models.RiskOutcome.evaluated_at.isnot(None),
                models.RiskOutcome.scored_at > cutoff,
                models.RiskOutcome.label_version == args.label_version,
            )
            .order_by(models.RiskOutcome.id)
            .all()
        )
        print(f"EVAL ROWS: {len(eval_rows)}")
        if args.expected_eval is not None and len(eval_rows) != args.expected_eval:
            print(f"ERROR: expected {args.expected_eval} eval rows, got {len(eval_rows)}")
            return 2

        # 3. Load Feature rows for exactly the eval (asset, decision_ts) pairs.
        pairs = {(row.asset_id, row.scored_at) for row in eval_rows}
        feature_rows = session.execute(
            select(
                models.Feature.asset_id,
                models.Feature.decision_ts,
                models.Feature.feature_name,
                models.Feature.feature_value,
                models.Feature.missing_flag,
            ).where(tuple_(models.Feature.asset_id, models.Feature.decision_ts).in_(list(pairs)))
        ).all()
        features_map: dict[tuple[int, datetime], dict[str, float]] = {}
        missing_map: dict[tuple[int, datetime], list[str]] = {}
        for asset_id, decision_ts, name, value, missing_flag in feature_rows:
            key = (asset_id, decision_ts)
            if missing_flag:
                missing_map.setdefault(key, []).append(name)
            else:
                features_map.setdefault(key, {})[name] = float(value)
        print(f"FEATURE ROWS: {len(feature_rows)} for {len(pairs)} eval pairs")

        # 4. Recompute risk per eval score; update only that field.
        updated = 0
        skipped_no_features = 0
        errors = 0
        conf_mismatches = 0
        risk_before_sum = 0.0
        risk_after_sum = 0.0
        err_before: list[float] = []
        err_after: list[float] = []
        unc: list[float] = []
        for index, outcome in enumerate(eval_rows, start=1):
            score = session.get(models.Score, outcome.score_id)
            if score is None:
                errors += 1
                continue
            key = (outcome.asset_id, outcome.scored_at)
            feat = features_map.get(key)
            if not feat:
                skipped_no_features += 1
                continue
            missing = missing_map.get(key, [])
            try:
                result = compute_scores(feat, missing, session=session)
            except Exception as exc:  # noqa: BLE001
                log.warning("apply_risk_calibration_error", score_id=score.id, error=str(exc))
                errors += 1
                continue
            collapsed = 1.0 if (outcome.collapsed or outcome.rugged) else 0.0
            risk_before_sum += score.risk or 0.0
            risk_after_sum += result.risk
            err_before.append(abs((score.risk or 0.0) / 100.0 - collapsed))
            err_after.append(abs(result.risk / 100.0 - collapsed))
            unc.append(score.uncertainty or 0.0)
            if abs((score.confidence or 0.0) - result.confidence) > 0.01:
                conf_mismatches += 1
            if args.yes:
                score.risk = result.risk
            updated += 1
            if index % 2000 == 0:
                print(f"  ... {index}/{len(eval_rows)} ({time.monotonic() - started:.0f}s elapsed)")
            if args.yes and updated % 500 == 0:
                session.commit()
        if args.yes:
            session.commit()

        n = max(updated, 1)
        print("SUMMARY")
        print(
            f"  updated={updated} skipped_no_features={skipped_no_features} "
            f"errors={errors} conf_mismatches={conf_mismatches} dry_run={not args.yes}"
        )
        print(f"  mean risk before={risk_before_sum / n:.2f} after={risk_after_sum / n:.2f}")
        print(
            f"  mean |risk/100 - collapsed| before={sum(err_before) / n:.4f} "
            f"after={sum(err_after) / n:.4f}"
        )
        print(
            f"  corr(uncertainty, risk error) before={_corr(unc, err_before):.4f} "
            f"after={_corr(unc, err_after):.4f}"
        )
        print(f"  elapsed={time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
