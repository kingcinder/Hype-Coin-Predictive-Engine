"""Versioned, structured report format for benchmark runs.

``ValidationReport`` is the canonical run-over-run artifact (design doc
§2.1.5): machine-readable JSON carrying partition metadata, per-cell metrics
with baselines and verdicts, and leakage flags. Console output is a derived
view, never the source of truth.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPORT_FORMAT_VERSION = 1


@dataclass
class MetricCell:
    """One metric for one output, in one regime window."""

    output: str  # e.g. risk_band, collapse_probability_24h, exit_risk
    metric: str  # brier / ece / precision@10 / concordance / ordinal_distance
    regime: str  # bull / bear / dead / mixed / unclassified
    n: int
    value: float
    ci_low: float
    ci_high: float
    baseline_value: float
    baseline_ci_low: float
    baseline_ci_high: float
    verdict: str  # better/indistinguishable/worse/insufficient_data
    leakage_suspected: bool = False
    note: str = ""
    # Number of independent assets behind an asset-bootstrapped cell. None for
    # row-level cells that do not use the asset-level verdict.
    n_assets: int | None = None

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        # B4 report key: the pre-registered schema calls the baseline "baseline"
        # while the original cell schema calls it "baseline_value". Emit both so
        # existing consumers and the pre-registration both read the same cell.
        data["baseline"] = self.baseline_value
        return data


@dataclass
class ValidationReport:
    report_format_version: int = REPORT_FORMAT_VERSION
    generated_at_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    git_sha: str = ""
    model_version: str = ""
    database_digest: dict[str, int] = field(default_factory=dict)
    partition: dict[str, object] = field(default_factory=dict)
    cells: list[MetricCell] = field(default_factory=list)
    suspicious_results: list[dict[str, object]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Outcome rows past their window that still have no established label.
    # They are withheld from every metric; the count makes the backlog visible.
    unresolved_outcomes: int = 0
    # Top-of-file label accounting (B4): confirmatory metrics are conditional
    # on resolved v2_bounded rows; v2 rows still unresolved are censored.
    resolved: int = 0
    censored: int = 0
    # v1_unbounded rows are retrospective/exploratory only and never enter the
    # confirmatory cells. This section carries their count and the explicit
    # "no confirmatory claim" text.
    retrospective_exploratory: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "report_format_version": self.report_format_version,
                "generated_at_utc": self.generated_at_utc,
                "git_sha": self.git_sha,
                "model_version": self.model_version,
                "database_digest": self.database_digest,
                "partition": self.partition,
                "cells": [cell.as_dict() for cell in self.cells],
                "suspicious_results": self.suspicious_results,
                "notes": self.notes,
                "unresolved_outcomes": self.unresolved_outcomes,
                "resolved": self.resolved,
                "censored": self.censored,
                "retrospective_exploratory": self.retrospective_exploratory,
            },
            indent=2,
            default=str,
        )

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json())
        return path
