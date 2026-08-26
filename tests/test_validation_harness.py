"""Permanent regression tests for the Phase 9 validation harness.

Covers the three pre-committed synthetic self-tests from
``docs/validation-harness-design.md`` §2.2 (perfect predictor, random noise,
injected leakage) plus unit checks on the core metric functions. These are the
guarantee that future changes to the harness cannot silently reintroduce a
broken or gameable metric.
"""

from __future__ import annotations

import numpy as np
import pytest

from validation.baselines import (
    base_rate_brier,
    base_rate_proportion,
    liquidity_depth_heuristic,
)
from validation.harness import ensemble_weight_tracking, evaluate_probabilities
from validation.leakage import check_feature_leakage
from validation.metrics import (
    BAND_RANKS,
    brier_score,
    concordance_index,
    confidence_calibration_error,
    expected_calibration_error,
    harrell_c_index,
    ordinal_band_distance,
    precision_at_k,
    wilson_ci,
)
from validation.selftest import (
    NOISE_BRIER_EXPECTED,
    NOISE_BRIER_TOL,
    NOISE_CONCORDANCE_TOL,
    run_self_tests,
)
from validation.synthetic import (
    make_leaked_dataset,
    make_noise_dataset,
    make_perfect_dataset,
)

# ── Design-doc §2.2 self-tests ──────────────────────────────────────────────


def test_self_test_1_perfect_predictor() -> None:
    d = make_perfect_dataset()
    probs, labels = d.probs, d.labels
    assert brier_score(probs, labels) == pytest.approx(0.0, abs=1e-9)
    assert expected_calibration_error(probs, labels) == pytest.approx(0.0, abs=1e-9)
    assert precision_at_k(probs, labels, 10) == pytest.approx(1.0, abs=1e-9)
    assert concordance_index(probs, labels) == pytest.approx(1.0, abs=1e-9)
    pred = np.where(probs >= 0.5, 4.0, 0.0)
    actual = np.where(labels == 1, 4.0, 0.0)
    assert ordinal_band_distance(pred, actual) == pytest.approx(0.0, abs=1e-9)
    leak = check_feature_leakage(d.feature_values, d.observed_at, d.decision_ts, labels)
    # Perfect but legitimate discrimination must NOT be confused with leakage.
    assert not leak.flagged


def test_self_test_2_random_noise_matches_naive_baseline() -> None:
    d = make_noise_dataset()
    probs, labels = d.probs, d.labels
    # Uniform(0,1) noise has expected Brier exactly 1/3 (design-doc erratum).
    assert brier_score(probs, labels) == pytest.approx(NOISE_BRIER_EXPECTED, abs=NOISE_BRIER_TOL)
    assert abs(concordance_index(probs, labels) - 0.5) <= NOISE_CONCORDANCE_TOL
    cells = evaluate_probabilities(
        probs, labels, output="collapse_probability_24h", regime="synthetic"
    )
    brier_verdict = next(c.verdict for c in cells if c.metric == "brier")
    precision_verdict = next(c.verdict for c in cells if c.metric == "precision@10")
    # Noise must never be reported as better than the naive baseline.
    assert brier_verdict != "better_than_baseline"
    assert precision_verdict == "indistinguishable_from_baseline"
    leak = check_feature_leakage(d.feature_values, d.observed_at, d.decision_ts, labels)
    assert not leak.flagged


def test_self_test_3_injected_leakage_is_flagged() -> None:
    d = make_leaked_dataset()
    probs, labels = d.probs, d.labels
    # The trap: raw metrics look perfect.
    assert concordance_index(probs, labels) >= 0.95
    leak = check_feature_leakage(d.feature_values, d.observed_at, d.decision_ts, labels)
    suspected_names = {f.name for f in leak.suspected}
    assert leak.flagged
    assert "L" in suspected_names
    assert "X" not in suspected_names


def test_all_self_tests_pass() -> None:
    results = run_self_tests()
    assert len(results) == 3
    for result in results:
        assert result.passed, f"self-test {result.case} failed: {result.leakage_reason}"


# ── Core metric correctness ─────────────────────────────────────────────────


def test_wilson_ci_extreme_proportions() -> None:
    # Wilson must not collapse to zero width at p=0 or p=1 (Wald failure mode).
    ci0 = wilson_ci(0, 30)
    assert ci0.low == 0.0
    assert ci0.high > 0.0
    ci1 = wilson_ci(30, 30)
    assert ci1.high == 1.0
    assert ci1.low < 1.0
    # CI at p=0.5 narrows with n.
    wide = wilson_ci(5, 10).high - wilson_ci(5, 10).low
    narrow = wilson_ci(50, 100).high - wilson_ci(50, 100).low
    assert narrow < wide


def test_concordance_index_known_values() -> None:
    # Perfect separation -> 1.0; reversed -> 0.0; random -> ~0.5.
    scores = np.array([0.1, 0.2, 0.3, 0.9, 0.8, 0.7])
    labels = np.array([0, 0, 0, 1, 1, 1])
    assert concordance_index(scores, labels) == pytest.approx(1.0)
    assert concordance_index(-scores, labels) == pytest.approx(0.0)
    rng = np.random.default_rng(0)
    noise = rng.random(200)
    noise_labels = (rng.random(200) < 0.3).astype(float)
    assert abs(concordance_index(noise, noise_labels) - 0.5) < 0.15


def test_harrell_c_index_censoring() -> None:
    # Event at t=2 should outrank a censored observation at t=10 (higher risk,
    # earlier event). Censored-vs-censored pairs never count.
    risk = np.array([3.0, 1.0, 2.0, 0.5])
    times = np.array([2.0, 10.0, 5.0, 10.0])
    events = np.array([1, 0, 1, 0])
    c = harrell_c_index(risk, times, events)
    assert 0.5 <= c <= 1.0
    # Perfect ordering among comparable pairs -> 1.0.
    risk2 = np.array([3.0, 2.0, 1.0])
    times2 = np.array([2.0, 4.0, 6.0])
    events2 = np.array([1, 1, 1])
    assert harrell_c_index(risk2, times2, events2) == pytest.approx(1.0)


def test_ordinal_distance_is_ordinal_aware() -> None:
    # A GREEN->YELLOW miss (distance 1) must cost less than GREEN->BLACK (4).
    one_off = ordinal_band_distance(np.array([1.0]), np.array([0.0]))
    far_off = ordinal_band_distance(np.array([4.0]), np.array([0.0]))
    assert one_off == pytest.approx(1.0)
    assert far_off == pytest.approx(4.0)
    assert len(BAND_RANKS) == 5  # GREEN..BLACK


def test_confidence_calibration_error() -> None:
    # Perfectly calibrated confidence: error ~0.
    conf = np.linspace(10, 90, 100)
    surv = conf / 100.0
    assert confidence_calibration_error(conf, surv) < 0.05
    # Miscalibrated: confidence 90 but survival 10% -> large error.
    surv_bad = np.full(100, 0.1)
    assert confidence_calibration_error(conf, surv_bad) > 0.2


def test_ensemble_weight_tracking_detects_drift() -> None:
    # A weight trajectory that tracks rising accuracy -> positive correlation.
    # Weights must VARY (a constant weight series has zero variance, so the
    # correlation is undefined — exactly what the harness must not report).
    history = [
        {"ts": f"2026-01-0{i}T00:00:00+00:00", "weights": {"rule": 0.4 + 0.1 * i, "ml": 0.3}}
        for i in range(1, 6)
    ]
    acc_series = {
        "rule": [
            {"ts": f"2026-01-0{i}T00:00:00+00:00", "accuracy": 0.4 + 0.1 * i} for i in range(1, 6)
        ]
    }
    corr = ensemble_weight_tracking(history, acc_series, scorer_names=("rule",))
    assert corr["rule"] > 0.5
    # Constant weight series -> undefined (nan), never a fabricated number.
    flat_history = [
        {"ts": f"2026-01-0{i}T00:00:00+00:00", "weights": {"rule": 0.5}} for i in range(1, 6)
    ]
    result = ensemble_weight_tracking(flat_history, acc_series, scorer_names=("rule",))
    assert np.isnan(result["rule"])
    # No history -> nan (insufficient_data upstream).
    assert np.isnan(ensemble_weight_tracking([], {}, scorer_names=("rule",))["rule"])


def test_liquidity_depth_heuristic_baseline() -> None:
    # Higher liquidity ranks first; when liquidity correlates with survival the
    # heuristic's concordance should exceed 0.5.
    rng = np.random.default_rng(1)
    liq = rng.random(200)
    labels = (liq > 0.7).astype(float)  # liquidity predicts survival
    result = liquidity_depth_heuristic(liq, labels, k=10)
    assert result["precision_at_k"] >= 0.5
    assert result["concordance"] > 0.6


def test_base_rate_brier() -> None:
    labels = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0, 1])
    assert base_rate_proportion(labels) == pytest.approx(0.1)
    assert base_rate_brier(labels) == pytest.approx(0.09)
