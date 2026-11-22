"""Fit the phase-conditioned confidence map out-of-sample and apply it to the
harness eval set on a LAB database copy — never the live DB.

Usage:
  SERPENT_DB_PATH=<lab copy> .venv/bin/python scripts/apply_phase_confidence.py \
      --cutoff 2026-10-20T20:45:55+00:00 [--expected-eval 19719] [--yes]

Steps:
  1. refresh_phase_confidence(session, cutoff, label_version="v2_bounded")
     — fits on the reference set (scored_at <= cutoff - 48h embargo) and
     installs the map in-process.
  2. Load the harness eval set: evaluated v2_bounded outcomes with
     scored_at > cutoff. Assert the count matches --expected-eval when given.
  3. For each eval score, recompute compute_scores from its Feature rows
     and update ONLY confidence / uncertainty / confidence_source in place.
     Risk, band, hype, ethos and every other field are left untouched, so
     the re-run harness measures exactly the confidence intervention.
  4. Commit in batches of 500 and print a before/after summary.

Default is a dry run (no writes); --yes performs the write pass. The lab DB
is a backup-API copy of the live DB; the live engine keeps running untouched.
"""

from __future__ import annotations

import argparse
import json
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
        help="ISO cutoff, e.g. 2026-10-20T20:45:55+00:00 (harness partition)",
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
        # 1. Fit the phase map on the reference set (out-of-sample by
        #    construction) and install it in-process.
        summary = refresh_phase_confidence(session, cutoff=cutoff, label_version=args.label_version)
        print("PHASE MAP FIT")
        print(
            json.dumps({k: v for k, v in summary.items() if k != "cells"}, indent=2, sort_keys=True)
        )
        for key in sorted(summary["cells"]):
            cell = summary["cells"][key]
            print(
                f"  {key}: conf={cell['confidence']:.2f} level={cell['level']} "
                f"cell_n={cell['cell_n']} cell_survived={cell['cell_survived']}"
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

        # 4. Recompute confidence/uncertainty per eval score; update only
        #    those three fields. Batch commits of 500.
        updated = 0
        skipped_no_features = 0
        errors = 0
        conf_before = 0.0
        conf_after = 0.0
        unc_before = 0.0
        unc_after = 0.0
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
                log.warning("apply_phase_confidence_error", score_id=score.id, error=str(exc))
                errors += 1
                continue
            conf_before += score.confidence or 0.0
            unc_before += score.uncertainty or 0.0
            if args.yes:
                score.confidence = result.confidence
                score.uncertainty = result.uncertainty
                score.confidence_source = result.confidence_source
            conf_after += result.confidence
            unc_after += result.uncertainty
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
            f"errors={errors} dry_run={not args.yes}"
        )
        print(f"  mean confidence before={conf_before / n:.2f} after={conf_after / n:.2f}")
        print(f"  mean uncertainty before={unc_before / n:.2f} after={unc_after / n:.2f}")
        print(f"  elapsed={time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
