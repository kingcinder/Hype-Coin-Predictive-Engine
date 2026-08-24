"""Data Quality Monitor — detects stale, missing, duplicate, and anomalous data.

Runs after each ingestion scan to ensure data flowing into the scoring
and forecast layers is trustworthy. Reports issues to the health dashboard.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from common.logging import get_logger
from common.time import utc_now

log = get_logger(__name__)

# Thresholds
STALE_PRICE_MINUTES = 10
MAX_DUPLICATE_RATIO = 0.15
ANOMALOUS_PRICE_CHANGE_PCT = 500.0  # 5x in one scan
MIN_REQUIRED_FIELDS = {"pair_id", "price_usd", "ts"}
MAX_NULL_RATIO = 0.30


@dataclass
class QualityIssue:
    """A single data quality finding."""
    category: str  # stale, missing, duplicate, anomalous
    severity: str  # warning, error, critical
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityReport:
    """Aggregated quality report for one scan pass."""
    checked: int = 0
    issues: list[QualityIssue] = field(default_factory=list)
    stale_count: int = 0
    missing_count: int = 0
    duplicate_count: int = 0
    anomalous_count: int = 0

    @property
    def ok(self) -> bool:
        return not any(i.severity == "critical" for i in self.issues)

    @property
    def summary(self) -> str:
        parts = []
        if self.stale_count:
            parts.append(f"{self.stale_count} stale")
        if self.missing_count:
            parts.append(f"{self.missing_count} missing")
        if self.duplicate_count:
            parts.append(f"{self.duplicate_count} duplicate")
        if self.anomalous_count:
            parts.append(f"{self.anomalous_count} anomalous")
        if not parts:
            return "all OK"
        return f"{len(self.issues)} issues: {', '.join(parts)}"


def check_market_snapshots(
    snapshots: list[dict[str, Any]],
    *,
    decision_ts: datetime | None = None,
) -> QualityReport:
    """Run quality checks on a batch of market snapshot records."""
    decision_ts = decision_ts or utc_now()
    report = QualityReport(checked=len(snapshots))
    seen_hashes: set[str] = set()

    for i, snap in enumerate(snapshots):
        # ── Missing required fields ──────────────────────────────────
        missing_fields = MIN_REQUIRED_FIELDS - set(snap.keys()) - {
            k for k in MIN_REQUIRED_FIELDS if snap.get(k) is not None
        }
        if missing_fields:
            report.missing_count += 1
            report.issues.append(QualityIssue(
                category="missing",
                severity="warning",
                message=f"Snapshot {i} missing fields: {missing_fields}",
                details={"index": i, "missing": list(missing_fields)},
            ))

        # ── Stale price ─────────────────────────────────────────────
        ts = snap.get("ts")
        if ts is not None:
            age = decision_ts - ts if isinstance(ts, datetime) else None
            if age and age > timedelta(minutes=STALE_PRICE_MINUTES):
                report.stale_count += 1
                report.issues.append(QualityIssue(
                    category="stale",
                    severity="warning",
                    message=f"Price is {age.total_seconds() / 60:.0f}min old (threshold {STALE_PRICE_MINUTES}min)",
                    details={"age_minutes": age.total_seconds() / 60, "pair_id": snap.get("pair_id")},
                ))

        # ── Anomalous price change ──────────────────────────────────
        price = snap.get("price_usd")
        prev_price = snap.get("previous_price_usd")
        if price is not None and prev_price is not None and prev_price > 0:
            change_pct = abs(float(price) - float(prev_price)) / float(prev_price) * 100
            if change_pct > ANOMALOUS_PRICE_CHANGE_PCT:
                report.anomalous_count += 1
                report.issues.append(QualityIssue(
                    category="anomalous",
                    severity="error" if change_pct > 1000 else "warning",
                    message=f"Price changed {change_pct:.0f}% in one scan",
                    details={"change_pct": change_pct, "pair_id": snap.get("pair_id")},
                ))

        # ── Null value ratio ────────────────────────────────────────
        null_count = sum(1 for v in snap.values() if v is None)
        ratio = null_count / max(1, len(snap))
        if ratio > MAX_NULL_RATIO:
            report.missing_count += 1
            report.issues.append(QualityIssue(
                category="missing",
                severity="warning",
                message=f"{ratio:.0%} null values in snapshot (threshold {MAX_NULL_RATIO:.0%})",
                details={"null_ratio": ratio, "pair_id": snap.get("pair_id")},
            ))

    # ── Duplicate detection ──────────────────────────────────────────
    for i, snap in enumerate(snapshots):
        key = f"{snap.get('pair_id', '')}_{snap.get('ts', '')}_{snap.get('source_id', '')}"
        h = hashlib.md5(key.encode()).hexdigest()[:12]
        if h in seen_hashes:
            report.duplicate_count += 1
            report.issues.append(QualityIssue(
                category="duplicate",
                severity="warning",
                message=f"Duplicate snapshot at index {i}",
                details={"index": i, "pair_id": snap.get("pair_id")},
            ))
        seen_hashes.add(h)

    dup_ratio = report.duplicate_count / max(1, len(snapshots))
    if dup_ratio > MAX_DUPLICATE_RATIO:
        report.issues.append(QualityIssue(
            category="duplicate",
            severity="error",
            message=f"Duplicate ratio {dup_ratio:.0%} exceeds threshold {MAX_DUPLICATE_RATIO:.0%}",
            details={"duplicate_ratio": dup_ratio},
        ))

    if report.ok:
        log.info("data_quality_check", checked=report.checked, result="ok")
    else:
        log.warning("data_quality_issues", checked=report.checked, issues=len(report.issues))

    return report
