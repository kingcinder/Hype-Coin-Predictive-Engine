"""Leakage-suspicion detection (design doc §2.2 case 3).

The exact failure mode the Phase 0 leakage audit chased: a feature whose value
is only knowable *after* the decision time looks like a perfect predictor.
The detector mirrors the engine's own point-in-time rule — a feature value is
legitimate only when ``observed_at <= decision_ts``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from validation.metrics import concordance_index

# A feature with this concordance (or better) and any point-in-time violation
# is flagged as suspected leakage — perfect-but-impossible discrimination.
LEAK_CONCORDANCE_THRESHOLD = 0.95


@dataclass
class FeatureMeta:
    """Point-in-time availability for one feature across samples."""

    name: str
    # concordance of this feature alone against the outcome (rank-AUC)
    concordance: float
    # fraction of non-missing values observed at or before their decision time
    availability_ratio: float
    violations: int = 0
    n_non_missing: int = 0


@dataclass
class LeakageReport:
    suspected: list[FeatureMeta] = field(default_factory=list)
    flagged: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "flagged": self.flagged,
            "reason": self.reason,
            "suspected_features": [
                {
                    "name": f.name,
                    "concordance": round(f.concordance, 4),
                    "availability_ratio": round(f.availability_ratio, 4),
                    "violations": f.violations,
                    "n_non_missing": f.n_non_missing,
                }
                for f in self.suspected
            ],
        }


def check_feature_leakage(
    feature_values: dict[str, np.ndarray],
    observed_at: dict[str, np.ndarray],
    decision_ts: np.ndarray,
    labels: np.ndarray,
    *,
    top_n: int = 3,
) -> LeakageReport:
    """Scan the top-``top_n`` features by concordance for point-in-time leaks.

    ``feature_values[name]``  → per-sample feature value (nan = missing)
    ``observed_at[name]``     → per-sample datetime the value was observed
    ``decision_ts``           → per-sample decision time
    ``labels``                → per-sample outcome (0/1)

    A feature is suspected when it has near-perfect concordance (>= 0.95) but
    at least one non-missing value was observed after its decision time.
    """
    report = LeakageReport()
    per_feature: list[FeatureMeta] = []
    n = len(labels)
    for name, values in feature_values.items():
        obs = observed_at.get(name)
        if obs is None or len(obs) != n:
            continue
        non_missing = ~np.isnan(np.asarray(values, dtype=float))
        n_non_missing = int(non_missing.sum())
        if n_non_missing < 10:
            continue
        conc = float("nan")
        try:
            conc = concordance_index(np.asarray(values, dtype=float), labels)
        except Exception:  # noqa: BLE001 - a single feature must not abort the scan
            conc = float("nan")
        if not np.isfinite(conc):
            continue
        obs_dt = np.asarray([_to_dt(v) for v in obs])
        dec_dt = np.asarray([_to_dt(v) for v in decision_ts])
        # Only non-missing rows can violate point-in-time ordering.
        late = ((obs_dt > dec_dt) & non_missing) if n_non_missing else np.zeros(n, dtype=bool)
        violations = int(late.sum())
        availability = (n_non_missing - violations) / n_non_missing if n_non_missing else 0.0
        per_feature.append(
            FeatureMeta(
                name=name,
                concordance=conc,
                availability_ratio=availability,
                violations=violations,
                n_non_missing=n_non_missing,
            )
        )

    ranked = sorted(per_feature, key=lambda f: f.concordance, reverse=True)
    for feature in ranked[:top_n]:
        if feature.concordance >= LEAK_CONCORDANCE_THRESHOLD and feature.violations > 0:
            report.suspected.append(feature)

    if report.suspected:
        report.flagged = True
        names = ", ".join(f.name for f in report.suspected)
        report.reason = (
            f"point-in-time leak suspected: {names} reach {LEAK_CONCORDANCE_THRESHOLD:.2f} "
            "concordance yet contain values observed after their decision time "
            "(availability ratio < 1.0). These cells must not be reported as genuine."
        )
    return report


def _to_dt(value: object) -> datetime:
    """Coerce datetime / ISO-string / numeric-epoch to a comparable datetime."""
    from datetime import UTC, datetime

    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return datetime.min.replace(tzinfo=UTC)
    return datetime.min.replace(tzinfo=UTC)
