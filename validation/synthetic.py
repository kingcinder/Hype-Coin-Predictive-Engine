"""Synthetic known-answer datasets for the three design-doc self-tests.

Pre-committed expectations live in ``docs/validation-harness-design.md`` §2.2.
These builders produce plain (prediction, outcome, feature) vectors so the
self-tests exercise the exact same metric code path as the real engine run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np


@dataclass
class SyntheticDataset:
    """One synthetic dataset with known answers."""

    name: str
    probs: np.ndarray  # predicted collapse probabilities
    labels: np.ndarray  # true outcomes (0/1)
    feature_values: dict[str, np.ndarray]  # name -> per-sample value (nan=missing)
    observed_at: dict[str, np.ndarray]  # name -> per-sample observation datetime
    decision_ts: np.ndarray  # per-sample decision datetime
    base_rate: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "n": int(len(self.labels)),
            "base_rate": self.base_rate,
        }


def _t0(n: int) -> np.ndarray:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return np.array([start + timedelta(hours=i) for i in range(n)], dtype=object)


def make_perfect_dataset(n: int = 500, base_rate: float = 0.10, seed: int = 1) -> SyntheticDataset:
    """Case 1: perfect, legitimate predictor.

    Feature ``X`` is a deterministic function of the outcome (perfect
    separation) and is observed 1h before the decision time — legitimate.
    """
    rng = np.random.default_rng(seed)
    labels = (rng.random(n) < base_rate).astype(float)
    probs = labels.astype(float)  # perfect probabilities 0.0 / 1.0
    t0 = _t0(n)
    x = labels + rng.normal(0.0, 1e-9, n)
    return SyntheticDataset(
        name="perfect",
        probs=probs,
        labels=labels,
        feature_values={"X": x},
        observed_at={"X": np.array([t - timedelta(hours=1) for t in t0], dtype=object)},
        decision_ts=t0,
        base_rate=base_rate,
    )


def make_noise_dataset(n: int = 500, base_rate: float = 0.10, seed: int = 7) -> SyntheticDataset:
    """Case 2: pure random noise at the true base rate.

    Predictions are Uniform(0,1) independent of the outcome.
    """
    rng = np.random.default_rng(seed)
    labels = (rng.random(n) < base_rate).astype(float)
    probs = rng.random(n)
    t0 = _t0(n)
    x = rng.normal(0.0, 1.0, n)
    return SyntheticDataset(
        name="noise",
        probs=probs,
        labels=labels,
        feature_values={"X": x},
        observed_at={"X": np.array([t - timedelta(hours=1) for t in t0], dtype=object)},
        decision_ts=t0,
        base_rate=base_rate,
    )


def make_leaked_dataset(n: int = 500, base_rate: float = 0.10, seed: int = 3) -> SyntheticDataset:
    """Case 3: deliberately injected leakage.

    Feature ``L`` is the outcome copied into a feature **observed 1h after the
    decision time** — exactly the failure mode the Phase 0 leakage audit was
    about. A second, legitimate feature ``X`` (observed before decision) has
    partial discrimination and must NOT be flagged.
    """
    rng = np.random.default_rng(seed)
    labels = (rng.random(n) < base_rate).astype(float)
    t0 = _t0(n)
    # Leaked feature: perfect copy of the outcome, observed AFTER decision.
    leaked = labels + rng.normal(0.0, 1e-9, n)
    # Legitimate feature: partial discrimination (moderate correlation), on time.
    legit = labels * 0.7 + rng.normal(0.0, 0.8, n)
    # The 'model' predicts using the leaked feature -> near-perfect probs.
    probs = np.clip(leaked, 0.0, 1.0)
    return SyntheticDataset(
        name="leaked",
        probs=probs,
        labels=labels,
        feature_values={"L": leaked, "X": legit},
        observed_at={
            "L": np.array([t + timedelta(hours=1) for t in t0], dtype=object),
            "X": np.array([t - timedelta(hours=1) for t in t0], dtype=object),
        },
        decision_ts=t0,
        base_rate=base_rate,
    )
