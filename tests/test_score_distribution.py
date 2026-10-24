from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from features.definitions import FEATURE_NAMES
from scoring.formulas import compute_scores
from scripts.score_distribution import ScoreSample, summarize_score_rows
from validation.metrics import concordance_index
from validation.selftest import NOISE_CONCORDANCE_TOL


def test_score_distribution_groups_histograms_by_decision_day() -> None:
    rows = [
        ScoreSample(datetime(2026, 10, 1, 9, tzinfo=UTC), 10.0, 20.0, 30.0),
        ScoreSample(datetime(2026, 10, 1, 10, tzinfo=UTC), 10.0, 22.0, 30.0),
        ScoreSample(datetime(2026, 10, 2, 9, tzinfo=UTC), 11.0, 22.0, None),
    ]

    report = summarize_score_rows(rows)

    first_day = report["2026-10-01"]["risk_score"]
    assert first_day == {
        "count": 2,
        "distinct_values": 1,
        "histogram": {"10": 2},
        "top_modes": [{"value": "10", "count": 2}],
    }
    assert report["2026-10-02"]["uncertainty"]["count"] == 0


def test_varied_inputs_preserve_real_risk_score_resolution() -> None:
    base = {name: 0.0 for name in FEATURE_NAMES}
    base.update(
        {
            "liquidity_depth": 20_000.0,
            "top_holder_concentration": 0.2,
            "pair_age_minutes": 1_000.0,
            "spread_estimate": 2.0,
            "buy_sell_ratio": 1.0,
            "holder_count": 500.0,
            "lifecycle_phase": 1.0,
        }
    )
    risk_scores = []
    for index in range(100):
        features = {
            **base,
            "liquidity_depth": 1_000.0 + index * 400.0,
            "top_holder_concentration": 0.2 + index * 0.006,
            "spread_estimate": 2.0 + index * 0.18,
            "volatility": 20.0 + index * 0.45,
        }
        risk_scores.append(compute_scores(features).risk)

    # 30 distinct outputs is a conservative floor: it decisively rejects the
    # old 2-4-value collapse while allowing unrelated input combinations to tie.
    assert len(set(risk_scores)) > 30


def test_formula_scores_on_independent_noise_stay_in_harness_band() -> None:
    rng = np.random.default_rng(20261023)
    features = {name: 0.0 for name in FEATURE_NAMES}
    features.update(
        {
            "liquidity_depth": 20_000.0,
            "pair_age_minutes": 1_000.0,
            "spread_estimate": 2.0,
            "buy_sell_ratio": 1.0,
            "holder_count": 500.0,
            "lifecycle_phase": 1.0,
        }
    )
    scores = []
    for _ in range(400):
        sample = {
            **features,
            "top_holder_concentration": float(rng.uniform(0.0, 0.95)),
            "volatility": float(rng.uniform(0.0, 80.0)),
            "spread_estimate": float(rng.uniform(0.0, 30.0)),
            "liquidity_depth": float(10 ** rng.uniform(2.5, 5.0)),
        }
        scores.append(compute_scores(sample).risk)
    noise_labels = rng.integers(0, 2, size=len(scores))

    measured = concordance_index(np.asarray(scores), noise_labels)

    assert abs(measured - 0.5) <= NOISE_CONCORDANCE_TOL
