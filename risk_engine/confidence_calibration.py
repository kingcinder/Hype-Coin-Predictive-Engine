"""Time-ordered confidence calibration from completed risk outcomes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from sklearn.isotonic import IsotonicRegression

from common.time import ensure_utc

MIN_VALIDATION_SAMPLES = 30


@dataclass(frozen=True)
class CalibrationSample:
    scored_at: datetime
    evaluated_at: datetime
    confidence: float
    survived: bool
    collapsed: bool
    risk_score: float


def chronological_split(
    samples: Sequence[CalibrationSample], *, validation_fraction: float = 0.2
) -> tuple[list[CalibrationSample], list[CalibrationSample], datetime | None]:
    """Split by score time and exclude training labels unknown at the split."""
    if not 0.0 < validation_fraction < 1.0:
        raise ValueError("validation_fraction must be between zero and one")
    ordered = sorted(samples, key=lambda row: ensure_utc(row.scored_at))
    if len(ordered) < 2:
        return [], [], None
    split_index = min(len(ordered) - 1, max(1, int(len(ordered) * (1 - validation_fraction))))
    split_at = ensure_utc(ordered[split_index].scored_at)
    train = [
        row
        for row in ordered
        if ensure_utc(row.scored_at) < split_at and ensure_utc(row.evaluated_at) <= split_at
    ]
    validation = [row for row in ordered if ensure_utc(row.scored_at) >= split_at]
    return train, validation, split_at


def fit_isotonic_map(
    confidence: Sequence[float],
    survived: Sequence[bool],
    *,
    min_samples: int,
) -> tuple[list[float], list[float]] | None:
    """Fit confidence-percent to survival-probability knots, or return None."""
    if len(confidence) != len(survived):
        raise ValueError("confidence and labels must have equal length")
    if len(confidence) < min_samples or len(set(survived)) < 2:
        return None
    x = np.clip(np.asarray(confidence, dtype=float) / 100.0, 0.0, 1.0)
    y = np.asarray(survived, dtype=float)
    model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    model.fit(x, y)
    return model.X_thresholds_.astype(float).tolist(), model.y_thresholds_.astype(float).tolist()


def apply_isotonic_map(
    confidence: float, x_points: Sequence[float], y_points: Sequence[float]
) -> float:
    """Return calibrated confidence in percent from persisted isotonic knots."""
    if not x_points or len(x_points) != len(y_points):
        raise ValueError("isotonic map must contain equally sized non-empty knots")
    probability = float(np.interp(np.clip(confidence / 100.0, 0.0, 1.0), x_points, y_points))
    return probability * 100.0


def retune_risk_thresholds(
    samples: Sequence[CalibrationSample],
    *,
    defaults: tuple[float, float, float] = (25.0, 50.0, 75.0),
) -> tuple[float, float, float]:
    """Derive risk-score band cutoffs from training rows only.

    Isotonic risk-to-collapse rates are inverted at the established 10/30/50%
    band boundaries. Defaults remain when there is no meaningful monotone fit.
    """
    if len(samples) < MIN_VALIDATION_SAMPLES or len({row.collapsed for row in samples}) < 2:
        return defaults
    mapping = fit_isotonic_map(
        [row.risk_score for row in samples],
        [row.collapsed for row in samples],
        min_samples=MIN_VALIDATION_SAMPLES,
    )
    if mapping is None:
        return defaults
    x_points, probabilities = mapping
    thresholds: list[float] = []
    for target in (0.10, 0.30, 0.50):
        indices = [i for i, value in enumerate(probabilities) if value >= target]
        if not indices:
            return defaults
        thresholds.append(round(100.0 * x_points[indices[0]], 2))
    if not thresholds[0] < thresholds[1] < thresholds[2]:
        return defaults
    return tuple(thresholds)  # type: ignore[return-value]
