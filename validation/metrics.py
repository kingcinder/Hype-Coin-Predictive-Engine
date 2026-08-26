"""Pure metric functions for the Phase 9 validation harness.

Numpy-only, no DB access — the same code path evaluates synthetic self-test
data and real engine rows. Implements the metrics fixed in
``docs/validation-methodology.md`` §2 and the verdict rules in §4.2.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Pre-committed (design doc §2.1.6 / methodology §4.2): below this sample size
# a metric cell is reported as insufficient_data, never averaged away.
MIN_SAMPLES_FOR_VERDICT = 30
# Methodology §4.2: trust threshold for calibration error.
CALIBRATION_TRUST_CEILING = 0.10


@dataclass(frozen=True)
class WilsonCI:
    low: float
    high: float

    def overlaps(self, other: WilsonCI) -> bool:
        return not (self.high < other.low or other.high < self.low)


def wilson_ci(k: float, n: int, z: float = 1.96) -> WilsonCI:
    """Wilson score interval for a binomial proportion (Wilson 1927).

    Used instead of the Wald interval: correct coverage at extreme
    proportions and small n (methodology §1.7).
    """
    if n <= 0:
        return WilsonCI(0.0, 0.0)
    p = float(k) / float(n)
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * np.sqrt(max(0.0, p * (1.0 - p) / n + z * z / (4.0 * n * n)))
    low = (centre - margin) / denom
    high = (centre + margin) / denom
    # A proportion can never be negative or exceed 1 — clamp the tiny
    # floating-point overshoot the formula produces at p=0 / p=1.
    return WilsonCI(float(max(0.0, low)), float(min(1.0, high)))


def brier_score(probs: np.ndarray, labels: np.ndarray) -> float:
    """Brier score: mean squared error of probability forecasts (Brier 1950)."""
    probs = np.asarray(probs, dtype=float)
    labels = np.asarray(labels, dtype=float)
    if len(labels) == 0:
        return float("nan")
    return float(np.mean((probs - labels) ** 2))


def brier_base_rate(base_rate: float) -> float:
    """Baseline Brier for a constant base-rate predictor: b(1-b)."""
    b = float(base_rate)
    return b * (1.0 - b)


def reliability_table(
    probs: np.ndarray, labels: np.ndarray, n_bins: int = 10
) -> list[dict[str, float]]:
    """Calibration table: per-bin mean predicted, observed frequency, weight.

    Matches the engine's own 10-bin ECE shape (forecast/engine.py).
    """
    probs = np.asarray(probs, dtype=float)
    labels = np.asarray(labels, dtype=float)
    if len(labels) == 0:
        return []
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    # right=True puts prob==1.0 in the last bin; clip guards probs slightly
    # outside [0, 1] after calibration.
    bin_idx = np.clip(np.digitize(probs, edges, right=True) - 1, 0, n_bins - 1)
    rows: list[dict[str, float]] = []
    for i in range(n_bins):
        mask = bin_idx == i
        count = int(mask.sum())
        if count == 0:
            continue
        mean_pred = float(probs[mask].mean())
        observed = float(labels[mask].mean())
        rows.append(
            {
                "bin": i,
                "bin_low": float(edges[i]),
                "bin_high": float(edges[i + 1]),
                "n": float(count),
                "mean_pred": mean_pred,
                "observed_freq": observed,
                "abs_error": abs(mean_pred - observed),
            }
        )
    return rows


def expected_calibration_error(probs: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> float:
    """ECE: weighted mean |predicted − observed| over probability bins."""
    rows = reliability_table(probs, labels, n_bins=n_bins)
    if not rows:
        return 1.0
    total = sum(row["n"] for row in rows)
    return float(sum(row["n"] * row["abs_error"] for row in rows) / total)


def precision_at_k(probs: np.ndarray, labels: np.ndarray, k: int) -> float:
    """Precision among the top-k by predicted probability (methodology §1.3).

    Ties at the k boundary are broken by stable argsort order — the same
    convention the engine's ``_precision_at_k`` uses.
    """
    probs = np.asarray(probs, dtype=float)
    labels = np.asarray(labels, dtype=float)
    if len(labels) == 0 or k <= 0:
        return 0.0
    order = np.argsort(probs)[::-1][: min(k, len(probs))]
    return float(labels[order].mean()) if len(order) else 0.0


def concordance_index(scores: np.ndarray, labels: np.ndarray) -> float:
    """Rank concordance (rank-AUC): P(random positive > random negative).

    Ties count 0.5. Equivalent to the ranking summary used for both
    classification and survival discrimination (methodology §1.3/§1.6).
    """
    scores = np.asarray(scores, dtype=float)
    labels = np.asarray(labels, dtype=float)
    pos = scores[labels == 1]
    neg = scores[labels == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    # Vectorized pair counting: for each positive, count strict > and == over
    # negatives. O(n_pos * n_neg) — fine for the engine's sample sizes.
    gt = int(np.sum(pos[:, None] > neg[None, :]))
    eq = int(np.sum(pos[:, None] == neg[None, :]))
    return (gt + 0.5 * eq) / float(len(pos) * len(neg))


def ordinal_band_distance(predicted_ranks: np.ndarray, actual_ranks: np.ndarray) -> float:
    """Mean absolute distance between predicted and actual band ranks (0..4).

    Ordinal-aware evaluation: GREEN→YELLOW (distance 1) is not equivalent to
    GREEN→BLACK (distance 4). Band order: GREEN=0, YELLOW=1, ORANGE=2, RED=3,
    BLACK=4 (methodology §2.1).
    """
    pred = np.asarray(predicted_ranks, dtype=float)
    actual = np.asarray(actual_ranks, dtype=float)
    if len(actual) == 0:
        return float("nan")
    return float(np.mean(np.abs(pred - actual)))


def harrell_c_index(risk_scores: np.ndarray, times: np.ndarray, events: np.ndarray) -> float:
    """Harrell's concordance index for right-censored survival data.

    ``risk_scores`` higher = higher predicted risk. ``times`` are observed
    times (event time for events, censoring time for censored); ``events`` is
    1/0. Comparable pairs: (event, event) always; (event, censored) only when
    the event time precedes the censoring time; (censored, censored) never.
    Risk ties count 0.5. This is the survival-analysis C-index the methodology
    §1.6/§2.4 requires — not binary accuracy on a collapsed/not-collapsed dummy.
    """
    risk = np.asarray(risk_scores, dtype=float)
    times = np.asarray(times, dtype=float)
    events = np.asarray(events, dtype=float)
    n = len(times)
    if n < 2:
        return float("nan")
    pairs = 0
    concordant = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            e_i, e_j = events[i], events[j]
            t_i, t_j = times[i], times[j]
            r_i, r_j = risk[i], risk[j]
            if e_i and e_j:
                if t_i == t_j:
                    continue  # tied event times are not comparable
                pairs += 1
                if (r_i > r_j and t_i < t_j) or (r_i < r_j and t_i > t_j):
                    concordant += 1.0
                elif r_i == r_j:
                    concordant += 0.5
            elif e_i and not e_j:
                if t_i < t_j:
                    pairs += 1
                    if r_i > r_j:
                        concordant += 1.0
                    elif r_i == r_j:
                        concordant += 0.5
            elif e_j and not e_i:
                if t_j < t_i:
                    pairs += 1
                    if r_j > r_i:
                        concordant += 1.0
                    elif r_j == r_i:
                        concordant += 0.5
    return concordant / pairs if pairs else float("nan")


def confidence_calibration_error(
    confidence: np.ndarray, survived: np.ndarray, n_bins: int = 10
) -> float:
    """ECE-style calibration of the confidence score (methodology §2.2).

    The engine's ``confidence`` (0..100) claims a fraction of predictions are
    reliable; the observable claim is the fraction of tokens that survive (do
    not collapse). Bucket confidence/100 and compare against the observed
    survival frequency, weighted by count.
    """
    conf = np.asarray(confidence, dtype=float) / 100.0
    surv = np.asarray(survived, dtype=float)
    if len(surv) == 0:
        return float("nan")
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_idx = np.clip(np.digitize(conf, edges, right=True) - 1, 0, n_bins - 1)
    total = 0.0
    weight = 0.0
    for i in range(n_bins):
        mask = bin_idx == i
        count = int(mask.sum())
        if count == 0:
            continue
        total += count * abs(float(conf[mask].mean()) - float(surv[mask].mean()))
        weight += count
    return total / weight if weight else float("nan")


def bootstrap_ci(
    x: np.ndarray,
    y: np.ndarray,
    metric_fn,
    *,
    n_boot: int = 400,
    seed: int = 7,
) -> tuple[float, float]:
    """Percentile bootstrap CI for a 2-array scalar metric.

    Deterministic (seeded) so reports are reproducible run-over-run. Used for
    metrics without a closed-form interval; observed proportions use the
    Wilson interval instead (methodology §1.7).
    """
    return bootstrap_ci_generic(
        metric_fn, np.asarray(x, dtype=float), np.asarray(y, dtype=float), n_boot=n_boot, seed=seed
    )


def bootstrap_ci_generic(
    metric_fn,
    *arrays: np.ndarray,
    n_boot: int = 400,
    seed: int = 7,
) -> tuple[float, float]:
    """Percentile bootstrap CI for a metric over any number of parallel arrays.

    ``metric_fn(*resampled_arrays)`` is evaluated on index-aligned resamples.
    Non-finite bootstrap values (e.g. a resample without positives) are
    dropped before the percentile, so a rare-event resample cannot poison the
    CI with NaN.
    """
    arrs = [np.asarray(a) for a in arrays]
    n = len(arrs[0]) if arrs else 0
    if n == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    values = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        values[i] = metric_fn(*[a[idx] for a in arrs])
    finite = values[np.isfinite(values)]
    if finite.size < max(10, n_boot // 4):
        return (float("nan"), float("nan"))
    return (float(np.percentile(finite, 2.5)), float(np.percentile(finite, 97.5)))


def verdict(
    value: float,
    ci: WilsonCI,
    baseline: float,
    baseline_ci: WilsonCI,
    *,
    higher_is_better: bool,
    n: int,
    min_samples: int = MIN_SAMPLES_FOR_VERDICT,
) -> str:
    """Apply methodology §4.2 verdict rules.

    - better_than_baseline: model CI lies entirely past the baseline point
      estimate AND the baseline CI's near edge (in the good direction).
    - worse_than_baseline: model CI entirely past the bad side of both.
    - indistinguishable_from_baseline: CIs overlap.
    - insufficient_data: n below the minimum or non-finite value.
    """
    if (
        n < min_samples
        or not np.isfinite(value)
        or not np.isfinite(ci.low)
        or not np.isfinite(ci.high)
        or not np.isfinite(baseline_ci.low)
        or not np.isfinite(baseline_ci.high)
    ):
        # A non-finite value OR non-finite CI bounds (e.g. a bootstrap that
        # degenerated on rare-event resamples) must yield insufficient_data,
        # never a misleading fall-through to indistinguishable_from_baseline.
        return "insufficient_data"
    lo, hi = ci.low, ci.high
    blo, bhi = baseline_ci.low, baseline_ci.high
    if higher_is_better:
        if lo > baseline and lo > bhi:
            return "better_than_baseline"
        if hi < baseline and hi < blo:
            return "worse_than_baseline"
    else:  # lower is better (brier, ECE, ordinal distance)
        if hi < baseline and hi < blo:
            return "better_than_baseline"
        if lo > baseline and lo > bhi:
            return "worse_than_baseline"
    return "indistinguishable_from_baseline"


BAND_RANKS = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3, "BLACK": 4}
