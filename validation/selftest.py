"""Self-test runner for the three pre-committed synthetic cases.

Design doc §2.2 — the harness must be proven correct on known-answer data
before it ever touches the real engine. ``run_self_tests()`` returns the raw
metric values; the pytest regression tests assert the pre-committed
expectations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from validation.harness import evaluate_probabilities
from validation.leakage import check_feature_leakage
from validation.metrics import (
    brier_score,
    concordance_index,
    expected_calibration_error,
    ordinal_band_distance,
    precision_at_k,
)
from validation.synthetic import (
    make_leaked_dataset,
    make_noise_dataset,
    make_perfect_dataset,
)

# Pre-committed pass bands from design doc §2.2 (corrected per erratum).
PERFECT_EPS = 1e-9
# Random Uniform(0,1) probs vs Bernoulli(b) outcomes: E[(p-y)^2] = 1/3 exactly.
NOISE_BRIER_EXPECTED = 1.0 / 3.0
NOISE_BRIER_TOL = 0.02
NOISE_CONCORDANCE_TOL = 0.10


@dataclass
class SelfTestResult:
    case: str
    metrics: dict[str, float] = field(default_factory=dict)
    verdict: str = ""
    leakage_flagged: bool = False
    leakage_reason: str = ""
    passed: bool = False


def _band_ranks_from_probs(probs: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Map perfect/noise probs to ordinal bands for the distance check."""
    # BLACK (4) when prob >= 0.5 & label positive, GREEN (0) otherwise — this
    # is the mapping used by the band-outcome evaluator (collapse->BLACK).
    pred = np.where(probs >= 0.5, 4.0, 0.0)
    actual = np.where(labels == 1, 4.0, 0.0)
    return pred, actual


def run_self_tests() -> list[SelfTestResult]:
    results: list[SelfTestResult] = []

    # ── Case 1: perfect predictor ────────────────────────────────────────────
    d = make_perfect_dataset()
    probs, labels = d.probs, d.labels
    metrics = {
        "brier": brier_score(probs, labels),
        "ece": expected_calibration_error(probs, labels),
        "precision_at_10": precision_at_k(probs, labels, 10),
        "concordance": concordance_index(probs, labels),
    }
    pred_ranks, actual_ranks = _band_ranks_from_probs(probs, labels)
    metrics["ordinal_distance"] = ordinal_band_distance(pred_ranks, actual_ranks)
    leak = check_feature_leakage(d.feature_values, d.observed_at, d.decision_ts, labels)
    cells = evaluate_probabilities(
        probs, labels, output="collapse_probability_24h", regime="synthetic"
    )
    passed = (
        abs(metrics["brier"]) <= PERFECT_EPS
        and abs(metrics["ece"]) <= PERFECT_EPS
        and abs(metrics["precision_at_10"] - 1.0) <= PERFECT_EPS
        and abs(metrics["concordance"] - 1.0) <= PERFECT_EPS
        and abs(metrics["ordinal_distance"]) <= PERFECT_EPS
        and not leak.flagged
    )
    results.append(
        SelfTestResult(
            case="1_perfect_predictor",
            metrics=metrics,
            verdict="perfect",
            leakage_flagged=leak.flagged,
            leakage_reason=leak.reason,
            passed=passed,
        )
    )

    # ── Case 2: pure random noise at the true base rate ─────────────────────
    d = make_noise_dataset()
    probs, labels = d.probs, d.labels
    metrics = {
        "brier": brier_score(probs, labels),
        "base_rate_brier": 0.09,
        "ece": expected_calibration_error(probs, labels),
        "precision_at_10": precision_at_k(probs, labels, 10),
        "concordance": concordance_index(probs, labels),
    }
    leak = check_feature_leakage(d.feature_values, d.observed_at, d.decision_ts, labels)
    cells = evaluate_probabilities(
        probs, labels, output="collapse_probability_24h", regime="synthetic"
    )
    brier_verdict = next(c.verdict for c in cells if c.metric == "brier")
    precision_verdict = next(c.verdict for c in cells if c.metric == "precision@10")
    passed = (
        abs(metrics["brier"] - NOISE_BRIER_EXPECTED) <= NOISE_BRIER_TOL
        and abs(metrics["concordance"] - 0.5) <= NOISE_CONCORDANCE_TOL
        # corrected erratum: the verdict is the assertion, not a tight band
        and precision_verdict == "indistinguishable_from_baseline"
        and brier_verdict != "better_than_baseline"
        and not leak.flagged
    )
    results.append(
        SelfTestResult(
            case="2_random_noise",
            metrics=metrics,
            verdict=f"brier={brier_verdict}, precision@{10}={precision_verdict}",
            leakage_flagged=leak.flagged,
            leakage_reason=leak.reason,
            passed=passed,
        )
    )

    # ── Case 3: injected leakage ────────────────────────────────────────────
    d = make_leaked_dataset()
    probs, labels = d.probs, d.labels
    metrics = {
        "brier": brier_score(probs, labels),
        "concordance": concordance_index(probs, labels),
        "precision_at_10": precision_at_k(probs, labels, 10),
    }
    leak = check_feature_leakage(d.feature_values, d.observed_at, d.decision_ts, labels)
    # The leaked feature L is observed after decision -> must be flagged.
    suspected_names = {f.name for f in leak.suspected}
    passed = (
        leak.flagged
        and "L" in suspected_names
        and "X" not in suspected_names
        and metrics["concordance"] >= 0.95  # the trap: near-perfect raw score
    )
    results.append(
        SelfTestResult(
            case="3_injected_leakage",
            metrics=metrics,
            verdict="leakage_suspected" if leak.flagged else "not_flagged",
            leakage_flagged=leak.flagged,
            leakage_reason=leak.reason,
            passed=passed,
        )
    )

    return results
