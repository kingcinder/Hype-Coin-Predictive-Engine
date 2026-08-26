"""CLI for the validation harness.

Usage:
    python -m validation --self-test            # run the 3 synthetic self-tests
    python -m validation --db serpent.db        # benchmark the real engine rows
    python -m validation --self-test --db ...   # both
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from validation.selftest import run_self_tests


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:  # noqa: BLE001 - not a git checkout.
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 9 validation harness")
    parser.add_argument("--self-test", action="store_true", help="run synthetic self-tests")
    parser.add_argument(
        "--db", type=str, default=None, help="path to engine DB (required unless --self-test)"
    )
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="report output path (default reports/validation-<ts>.json)",
    )
    parser.add_argument("--min-samples", type=int, default=30, help="minimum samples for a verdict")
    args = parser.parse_args(argv)

    if not args.self_test and not args.db:
        parser.print_help()
        return 1

    if args.self_test:
        results = run_self_tests()
        all_passed = True
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            all_passed = all_passed and result.passed
            print(
                f"[{status}] case={result.case} verdict={result.verdict} "
                f"leakage_flagged={result.leakage_flagged}"
            )
            metrics_str = json.dumps({k: round(v, 4) for k, v in result.metrics.items()})
            print(f"         metrics={metrics_str}")
            if not result.passed:
                print(f"         reason: {result.leakage_reason or 'expectation mismatch'}")
        if not all_passed:
            print("SELF-TESTS FAILED — debug the harness, not the expectations.", file=sys.stderr)
            return 2
        print("ALL SELF-TESTS PASSED")
        if not args.db:
            return 0

    if args.db:
        # Read-only contract (design doc §2.1.2): never create or alter engine
        # tables — the engine DB already has its full schema.
        from sqlalchemy.orm import sessionmaker

        from storage.database import make_engine

        db_url = args.db
        if not (args.db.startswith("sqlite") or "://" in args.db):
            db_url = f"sqlite+pysqlite:///{args.db}"
        engine = make_engine(database_url=db_url)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with SessionLocal() as session:
            from validation.harness import run_harness

            report = run_harness(
                session,
                git_sha=_git_sha(),
                model_version="engine-current",
                min_samples=args.min_samples,
            )
        ts_key = report.generated_at_utc.replace(":", "").replace("+", "_")
        out = args.out or f"reports/validation-{ts_key}.json"
        path = report.save(out)
        print(f"REPORT WRITTEN: {path}")
        print(f"  cells={len(report.cells)} suspicious={len(report.suspicious_results)}")
        for cell in report.cells:
            if cell.verdict != "insufficient_data":
                print(
                    f"  {cell.output} {cell.metric} [{cell.regime}] n={cell.n} "
                    f"value={cell.value:.4f} CI=({cell.ci_low:.4f},{cell.ci_high:.4f}) "
                    f"baseline={cell.baseline_value:.4f} -> {cell.verdict}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
