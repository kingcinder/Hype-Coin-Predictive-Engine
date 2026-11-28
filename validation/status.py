"""Trading-facing summary of whether the risk scores are validated.

The engine's scores are research outputs, not validated predictors. Anything
that offers a trading decision must say so explicitly, so this module derives
one flag + reason from the newest ``validation-*.json`` report and the API
surfaces it verbatim. The headline cells can be beaten by dead-token
structure alone, so the flag additionally requires a live-token edge: BLACK
collapse precision on tokens that were live at score time must beat the live
base rate outright.
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
    ("risk_band:live:BLACK", "collapse_precision", "BLACK collapse precision (live tokens)"),
)

# Outputs whose cell must *beat* its baseline outright: merely
# "indistinguishable" is not enough. The headline BLACK beat is driven by
# dead-token structure (dead tokens stay dead), so the live-token read is the
# honest edge check -- a live precision that does not clear the live base rate
# with its whole bootstrap CI above it must not validate the scores.
_STRICT_BEAT_OUTPUTS: frozenset[str] = frozenset({"risk_band:live:BLACK"})


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
    is at least indistinguishable from / better than its baseline -- except the
    live-token BLACK cell, which must beat the live base rate outright. A
    report that passes the headline cells on dead-token structure alone does
    not validate the scores.
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
        elif output in _STRICT_BEAT_OUTPUTS and verdict not in {
            "beats_baseline",
            "better_than_baseline",
        }:
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


# Entry-mission gate (Task 6, spec §6): the entry-side mirror of
# _REQUIRED_CELLS. p_ignition calibration must merely clear its bar, but
# the live-stratum entry cells must *beat* their baselines outright — an
# entry ranking that only matches the live base hit rate has no edge.
_ENTRY_REQUIRED_CELLS: tuple[tuple[str, str, str], ...] = (
    ("p_ignition_24h", "calibration_error", "p_ignition calibration"),
    ("entry_priority:live", "precision_at_20", "entry precision@20 (live tokens)"),
    ("entry_priority:live", "gain_lift_top_decile", "entry gain lift (live tokens)"),
)

_ENTRY_STRICT_BEAT_OUTPUTS: frozenset[str] = frozenset({"entry_priority:live"})


def entry_validation_status(reports_dir: Path | None = None) -> dict[str, Any]:
    """Return ``entry_validated`` plus a reason taken from the newest report.

    Structural copy of :func:`scores_validation_status` over the entry
    cells: the flag is ``True`` only when every required metric has
    enough samples and clears its baseline, with the live-stratum cells
    required to beat outright. A report carrying the forecast-freshness
    cell as ``insufficient_data`` fails immediately with the
    stale-forecasts reason, ahead of any per-cell problem list — stale
    forecasts are never read as a current entry signal.
    """
    directory = REPORTS_DIR if reports_dir is None else Path(reports_dir)
    reports = sorted(directory.glob("validation-*.json"))
    if not reports:
        return {
            "entry_validated": False,
            "entry_validation_report": None,
            "entry_validation_reason": "no validation report has been generated",
        }

    # The report's own timestamp, not the filename (see scores gate).
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
            "entry_validated": False,
            "entry_validation_report": name,
            "entry_validation_reason": f"latest validation report is unreadable ({name})",
        }
    _, report_path, report = max(loaded, key=lambda item: item[0])

    raw_cells = report.get("cells")
    cells = (
        [cell for cell in raw_cells if isinstance(cell, dict)]
        if isinstance(raw_cells, list)
        else []
    )

    # Freshness precondition (spec §3/§10) takes precedence over every
    # per-cell problem: this report's entry data was stale when generated.
    freshness = _find_cell(cells, "forecasts", "freshness")
    if freshness is not None and str(freshness.get("verdict") or "") == "insufficient_data":
        note = str(freshness.get("note") or "")
        reason = (
            note
            if note.startswith("stale forecasts")
            else (
                "stale forecasts: newest forecast decision_ts unknown is older "
                "than forecast_max_staleness_hours"
            )
        )
        return {
            "entry_validated": False,
            "entry_validation_report": report_path.name,
            "entry_validation_reason": reason,
        }

    problems: list[str] = []
    for output, metric, label in _ENTRY_REQUIRED_CELLS:
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
        elif output in _ENTRY_STRICT_BEAT_OUTPUTS and verdict not in {
            "beats_baseline",
            "better_than_baseline",
        }:
            problems.append(
                f"{label} {_fmt(cell.get('value'))} "
                f"vs {_fmt(cell.get('baseline_value'))} ({verdict})"
            )

    if problems:
        resolved = _find_cell(cells, "entry_priority:live", "precision_at_20")
        n = resolved.get("n") if resolved else "unknown"
        reason = (
            f"entry signals are not validated ({report_path.name}): "
            + "; ".join(problems)
            + f"; n={n} eval entry rows"
        )
        return {
            "entry_validated": False,
            "entry_validation_report": report_path.name,
            "entry_validation_reason": reason,
        }

    return {
        "entry_validated": True,
        "entry_validation_report": report_path.name,
        "entry_validation_reason": (
            f"all required entry metrics clear their baselines ({report_path.name})"
        ),
    }


# Exit-mission gate (Task 8, spec §6): the exit-side mirror of
# _REQUIRED_CELLS. Every required cell sits on the single exit_policy
# output and every one must *beat* the fixed-ladder baseline outright —
# the captured-fraction cell must strict-beat the ladder, and BOTH the
# median and the p25 captured-fraction cells must strict-beat theirs.
# A mean-only (or median-only) win does not open the gate: a policy
# that helps typical trades and hurts bad ones fails where the money
# is lost.
_EXIT_REQUIRED_CELLS: tuple[tuple[str, str, str], ...] = (
    ("exit_policy", "captured_fraction", "exit captured fraction vs ladder"),
    ("exit_policy", "median_captured_fraction", "exit median captured fraction vs ladder"),
    ("exit_policy", "p25_captured_fraction", "exit p25 captured fraction vs ladder"),
)

_EXIT_STRICT_BEAT_OUTPUTS: frozenset[str] = frozenset({"exit_policy"})


def exit_validation_status(reports_dir: Path | None = None) -> dict[str, Any]:
    """Return ``exit_validated`` plus a reason taken from the newest report.

    Structural copy of :func:`entry_validation_status` over the exit
    cells: the flag is ``True`` only when every required captured-
    fraction cell has enough samples and beats its fixed-ladder
    baseline outright — mean, median, AND p25. Until Task 9 wires real
    exit rows into the harness, reports carry no passing exit cells
    and the gate honestly reads ``False``.
    """
    directory = REPORTS_DIR if reports_dir is None else Path(reports_dir)
    reports = sorted(directory.glob("validation-*.json"))
    if not reports:
        return {
            "exit_validated": False,
            "exit_validation_report": None,
            "exit_validation_reason": "no validation report has been generated",
        }

    # The report's own timestamp, not the filename (see scores gate).
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
            "exit_validated": False,
            "exit_validation_report": name,
            "exit_validation_reason": f"latest validation report is unreadable ({name})",
        }
    _, report_path, report = max(loaded, key=lambda item: item[0])

    raw_cells = report.get("cells")
    cells = (
        [cell for cell in raw_cells if isinstance(cell, dict)]
        if isinstance(raw_cells, list)
        else []
    )
    problems: list[str] = []
    for output, metric, label in _EXIT_REQUIRED_CELLS:
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
        elif output in _EXIT_STRICT_BEAT_OUTPUTS and verdict not in {
            "beats_baseline",
            "better_than_baseline",
        }:
            problems.append(
                f"{label} {_fmt(cell.get('value'))} "
                f"vs {_fmt(cell.get('baseline_value'))} ({verdict})"
            )

    if problems:
        resolved = _find_cell(cells, "exit_policy", "captured_fraction")
        n = resolved.get("n") if resolved else "unknown"
        reason = (
            f"exit policy is not validated ({report_path.name}): "
            + "; ".join(problems)
            + f"; n={n} eval exit rows"
        )
        return {
            "exit_validated": False,
            "exit_validation_report": report_path.name,
            "exit_validation_reason": reason,
        }

    return {
        "exit_validated": True,
        "exit_validation_report": report_path.name,
        "exit_validation_reason": (
            f"all required exit metrics beat their ladder baselines ({report_path.name})"
        ),
    }
