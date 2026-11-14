"""Trading-facing summary of whether the risk scores are validated.

The engine's scores are research outputs, not validated predictors: the latest
report's risk-band ranking and confidence calibration are worse than their
baselines. Anything that offers a trading decision must say so explicitly, so
this module derives one flag + reason from the newest ``validation-*.json``
report and the API surfaces it verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"

# Metrics that must clear their verdicts before scores count as validated.
_REQUIRED_CELLS: tuple[tuple[str, str, str], ...] = (
    ("risk_band", "ordinal_distance", "RiskBand ranking"),
    ("confidence", "calibration_error", "confidence calibration"),
    ("risk_band:BLACK", "collapse_precision", "BLACK collapse precision"),
)


def _find_cell(cells: list[dict[str, Any]], output: str, metric: str) -> dict[str, Any] | None:
    for cell in cells:
        if cell.get("output") == output and cell.get("metric") == metric:
            return cell
    return None


def _fmt(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "n/a"


def scores_validation_status(reports_dir: Path | None = None) -> dict[str, Any]:
    """Return ``scores_validated`` plus a reason taken from the newest report.

    The flag is ``True`` only when every required metric has enough samples and
    is at least indistinguishable from / better than its baseline. Today's
    report fails, so trading payloads must show the warning rather than a
    clean bill of health.
    """
    directory = REPORTS_DIR if reports_dir is None else Path(reports_dir)
    reports = sorted(directory.glob("validation-*.json"))
    if not reports:
        return {
            "scores_validated": False,
            "scores_validation_report": None,
            "scores_validation_reason": "no validation report has been generated",
        }

    # The report's own timestamp, not the filename: hand-named reports (e.g.
    # ``...-v2-relabel.json``) can be newer than the CLI's ``T…`` names.
    loaded: list[tuple[str, Path, dict[str, Any]]] = []
    unreadable: list[str] = []
    for path in reports:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            unreadable.append(path.name)
            continue
        if isinstance(report, dict):
            loaded.append((str(report.get("generated_at_utc") or ""), path, report))
    if not loaded:
        name = reports[-1].name
        return {
            "scores_validated": False,
            "scores_validation_report": name,
            "scores_validation_reason": f"latest validation report is unreadable ({name})",
        }
    _, report_path, report = max(loaded, key=lambda item: item[0])

    raw_cells = report.get("cells")
    cells = (
        [cell for cell in raw_cells if isinstance(cell, dict)]
        if isinstance(raw_cells, list)
        else []
    )
    problems: list[str] = []
    for output, metric, label in _REQUIRED_CELLS:
        cell = _find_cell(cells, output, metric)
        if cell is None:
            problems.append(f"{label}: missing")
            continue
        verdict = str(cell.get("verdict") or "unknown")
        if verdict in {"worse_than_baseline", "insufficient_data", "unknown"}:
            problems.append(
                f"{label} {_fmt(cell.get('value'))} "
                f"vs {_fmt(cell.get('baseline_value'))} ({verdict})"
            )

    if problems:
        resolved = _find_cell(cells, "risk_band", "ordinal_distance")
        n = resolved.get("n") if resolved else "unknown"
        reason = (
            f"scores are not validated ({report_path.name}): "
            + "; ".join(problems)
            + f"; n={n} resolved outcomes"
        )
        return {
            "scores_validated": False,
            "scores_validation_report": report_path.name,
            "scores_validation_reason": reason,
        }

    return {
        "scores_validated": True,
        "scores_validation_report": report_path.name,
        "scores_validation_reason": (
            f"all required metrics clear their baselines ({report_path.name})"
        ),
    }
