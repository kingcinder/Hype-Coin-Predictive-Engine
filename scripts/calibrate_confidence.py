"""Fit and persist a point-in-time confidence calibrator from RiskOutcome rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.time import utc_now
from risk_engine.calibrator import run_confidence_calibration
from storage.database import run_migrations, session_scope


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="calibrator version label")
    parser.add_argument("--out", type=Path, help="write the JSON result to this path")
    args = parser.parse_args()

    now = utc_now()
    version = args.version or f"isotonic-confidence-{now:%Y%m%d}"
    run_migrations()
    with session_scope() as session:
        result = run_confidence_calibration(session, version=version, trained_at=now)
        session.commit()

    payload = {
        "generated_at_utc": now.isoformat(),
        "version": result.version,
        "status": result.status,
        "fallback_active": result.status != "active",
        "sample_size": result.sample_size,
        "validation_sample_size": result.validation_sample_size,
        "training_started_at": result.training_started_at.isoformat()
        if result.training_started_at
        else None,
        "training_ended_at": result.training_ended_at.isoformat()
        if result.training_ended_at
        else None,
        "validation_started_at": result.validation_started_at.isoformat()
        if result.validation_started_at
        else None,
        "baseline_ece": result.baseline_ece,
        "calibrated_ece": result.calibrated_ece,
        "ece_trust_ceiling": 0.10,
        "training_thresholds": list(result.thresholds) if result.thresholds else None,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
