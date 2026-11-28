#!/usr/bin/env python3
"""Snapshot-coverage report — the Task 0 gate artifact.

Measures forward-window snapshot density for forecasted/scored assets
(``ingestion.coverage.snapshot_coverage``: the label density rule itself,
judged per decision point) and writes
``reports/snapshot-coverage-<YYYYMMDD>.json`` + ``.md``.

Exit code: 0 iff ``pass_share >= settings.snapshot_coverage_min_pass_share``
(0.80), else 1. The report is written either way. This is a *live-data*
gate: the 24h forward windows it judges need a day of fixed-cadence
collection to accrue, so it cannot pass on the day the cadence fix lands.

Usage:
  python scripts/snapshot_coverage_report.py [--as-of <ISO-8601>]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.config import Settings, get_settings  # noqa: E402
from common.time import ensure_utc, utc_now  # noqa: E402
from ingestion.coverage import CoverageReport, snapshot_coverage  # noqa: E402
from storage.database import session_scope  # noqa: E402

REPORTS_DIR = REPO / "reports"


def _render_markdown(report: CoverageReport, *, min_pass_share: float, gate_passed: bool) -> str:
    return "\n".join(
        [
            f"# Snapshot coverage — {report.as_of.date().isoformat()}",
            "",
            f"- as_of: {report.as_of.isoformat()}",
            f"- forward window: {report.window_hours}h",
            f"- assets measured: {report.assets_measured}",
            f"- assets meeting density: {report.assets_meeting_density}",
            f"- pass share: {report.pass_share:.4f} (gate: >= {min_pass_share:.2f})",
            f"- median snapshots / asset / day: {report.median_snapshots_per_asset_day:.2f}",
            f"- p10 snapshots / asset / day: {report.p10_snapshots_per_asset_day:.2f}",
            f"- gate: {'PASS' if gate_passed else 'FAIL'}",
            "",
            "Density is the label rule (forecast.labels.forward_evidence_sufficient):",
            ">= ceil(window / 6h) snapshots in the forward window, one in the final",
            "quartile, counting only snapshots observed by as_of.",
            "",
        ]
    )


def write_coverage_report(
    *,
    session=None,
    settings: Settings | None = None,
    as_of: datetime | None = None,
    reports_dir: Path | None = None,
) -> int:
    """Write the coverage report; return the gate exit code (0 pass, 1 fail).

    ``session`` is an injectable seam (keyword-only): when None — the CLI
    path — this call opens and owns its own ``session_scope()`` cycle.
    """
    settings = settings if settings is not None else get_settings()
    as_of = ensure_utc(as_of) if as_of is not None else utc_now()
    out_dir = reports_dir if reports_dir is not None else REPORTS_DIR

    if session is not None:
        report = snapshot_coverage(session, settings=settings, as_of=as_of)
    else:
        with session_scope() as active:
            report = snapshot_coverage(active, settings=settings, as_of=as_of)

    gate_passed = report.pass_share >= settings.snapshot_coverage_min_pass_share
    payload = asdict(report)
    payload["as_of"] = report.as_of.isoformat()
    payload["min_pass_share"] = settings.snapshot_coverage_min_pass_share
    payload["gate_passed"] = gate_passed

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.as_of.strftime("%Y%m%d")
    json_path = out_dir / f"snapshot-coverage-{stamp}.json"
    md_path = out_dir / f"snapshot-coverage-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(
        _render_markdown(
            report,
            min_pass_share=settings.snapshot_coverage_min_pass_share,
            gate_passed=gate_passed,
        ),
        encoding="utf-8",
    )
    print(
        f"snapshot coverage: {report.assets_meeting_density}/{report.assets_measured} "
        f"assets meet density (pass_share={report.pass_share:.4f}, "
        f"gate >= {settings.snapshot_coverage_min_pass_share:.2f}) -> "
        f"{'PASS' if gate_passed else 'FAIL'}; wrote {json_path} + {md_path}"
    )
    return 0 if gate_passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot-coverage report (Task 0 gate)")
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 timestamp to measure as of (default: now, UTC)",
    )
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    return write_coverage_report(as_of=as_of)


if __name__ == "__main__":
    sys.exit(main())
