"""Naive baselines the engine's outputs must beat (methodology §4.1).

Every baseline is a plain function over (prediction, outcome) vectors so the
synthetic self-tests exercise the identical code path as the real run.
"""

from __future__ import annotations

import numpy as np

from validation.metrics import (
    WilsonCI,
    brier_base_rate,
    concordance_index,
    precision_at_k,
    wilson_ci,
)


def base_rate_proportion(labels: np.ndarray) -> float:
    labels = np.asarray(labels, dtype=float)
    return float(labels.mean()) if len(labels) else float("nan")


def base_rate_brier(labels: np.ndarray) -> float:
    """Baseline Brier: predict the base rate everywhere."""
    return brier_base_rate(base_rate_proportion(labels))


def base_rate_precision_at_k(labels: np.ndarray, k: int) -> float:
    """Random-ranking precision@k expectation: ≈ base rate.

    A random ranker picks positives at the base rate, so expected precision@k
    is b (with hypergeometric variance). Returned as a point estimate.
    """
    labels = np.asarray(labels, dtype=float)
    return precision_at_k(labels, labels, k) if len(labels) else float("nan")


def base_rate_wilson_ci(labels: np.ndarray, *, z: float = 1.96) -> WilsonCI:
    """Wilson CI around the base rate — the significance band a real model's
    precision@k must escape to be 'better than random ranking'."""
    labels = np.asarray(labels, dtype=float)
    k = float(labels.sum())
    return wilson_ci(k, len(labels), z=z)


def random_topk_precision_ci(
    labels: np.ndarray, k: int, *, n_sim: int = 2000, seed: int = 11
) -> WilsonCI:
    """Null distribution for precision@k under random ranking.

    A random ranker's top-k is a hypergeometric draw, not a fresh binomial
    sample: with n=500, b=0.10 and k=10, the top-10 contains 0 positives with
    probability ~0.35. Comparing a model's single precision@k draw against the
    base-rate Wilson CI (the CI of the *rate itself*) therefore produces false
    'worse' verdicts. The correct null is the simulated distribution of
    random-top-k precision; the model's value is 'indistinguishable from
    baseline' when it falls inside this band.
    """
    labels = np.asarray(labels, dtype=float)
    n = len(labels)
    k_eff = min(k, n)
    if n == 0 or k_eff == 0:
        return WilsonCI(0.0, 0.0)
    rng = np.random.default_rng(seed)
    values = np.empty(n_sim)
    for i in range(n_sim):
        shuffled = rng.permutation(labels)[:k_eff]
        values[i] = float(shuffled.mean()) if len(shuffled) else 0.0
    return WilsonCI(float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5)))


def random_ranking_concordance(n: int) -> float:
    """Expected concordance of an uninformative ranker: 0.5."""
    return 0.5


def liquidity_depth_heuristic(
    liquidity: np.ndarray, labels: np.ndarray, *, k: int = 10
) -> dict[str, float]:
    """Single-feature baseline: rank by liquidity depth only.

    The methodology mandates comparing the engine against a dumb heuristic
    ('liquidity depth only') so a mediocre model cannot look impressive with
    no comparison point. Higher liquidity ranked first (assumes depth predicts
    survival — the heuristic's whole claim).
    """
    liquidity = np.asarray(liquidity, dtype=float)
    labels = np.asarray(labels, dtype=float)
    n = len(labels)
    if n == 0:
        return {"precision_at_k": float("nan"), "concordance": float("nan")}
    prec = precision_at_k(liquidity, labels, min(k, n))
    conc = concordance_index(liquidity, labels)
    return {"precision_at_k": prec, "concordance": conc}
