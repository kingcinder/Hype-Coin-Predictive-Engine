"""Publication guard for validation metrics surfaced outside the harness."""

from __future__ import annotations

from typing import Any

_NEAR_PERFECT = 0.95


def guard_validation_cells(report: dict[str, Any]) -> dict[str, Any]:
    cells = report.get("cells") or []
    suspicious = report.get("suspicious_results") or []
    audit_cell = next(
        (
            cell
            for cell in cells
            if cell.get("output") == "feature_leakage"
            and cell.get("metric") == "suspected_features"
            and int(cell.get("n") or 0) > 0
        ),
        None,
    )
    audit_ran = audit_cell is not None
    findings = {
        (item.get("output"), item.get("metric")) for item in suspicious if isinstance(item, dict)
    }
    guarded: list[dict[str, Any]] = []
    for source in cells:
        cell = dict(source)
        cell["status"] = cell.get("verdict", "unknown")
        cell["guard_note"] = None
        value = cell.get("value")
        if cell.get("metric") == "concordance" and isinstance(value, (int, float)):
            if value >= _NEAR_PERFECT and not audit_ran:
                cell["value"] = None
                cell["status"] = "suppressed_missing_crosscheck"
                cell["guard_note"] = (
                    "Near-perfect concordance suppressed because the report has no "
                    "point-in-time leakage cross-check."
                )
            elif value >= _NEAR_PERFECT:
                if (cell.get("output"), cell.get("metric")) in findings:
                    cell["status"] = "flagged_suspicious"
                    cell["guard_note"] = (
                        "Near-perfect concordance was flagged by the report cross-check; "
                        "do not interpret it as predictive skill."
                    )
                else:
                    cell["status"] = "cross_checked"
        guarded.append(cell)
    return {"leakage_crosscheck_ran": audit_ran, "cells": guarded}
