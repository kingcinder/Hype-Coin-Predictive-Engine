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

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


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
            },
            indent=2,
            default=str,
        )

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json())
        return path
