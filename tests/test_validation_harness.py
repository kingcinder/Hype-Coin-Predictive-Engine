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


# ── run_harness leakage-audit wiring (H24) ──────────────────────────────────


def test_run_harness_wires_feature_leakage_audit(session) -> None:
    """H24: run_harness must invoke the real point-in-time leakage detector on
    the engine's persisted Feature rows — a leaking feature (perfectly
    concordant, observed after its decision time) must surface in the
    feature_leakage cell and in suspicious_results."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from storage import models
    from storage.repository import upsert_asset
    from tests.conftest import seed_reference
    from validation.harness import run_harness

    chain, _source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Leak111111111111111111111111111111111111",
        symbol="LEAK",
        name="Leak Fixture",
        first_seen_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    base = datetime(2026, 5, 2, tzinfo=UTC)
    n = 12
    for hour in range(n):
        decision_ts = base + timedelta(hours=hour)
        collapsed = hour % 2 == 0
        score = models.Score(
            asset_id=asset.id,
            decision_ts=decision_ts,
            observed_at=decision_ts,
            hype=0.5,
            ethos=0.5,
            risk=0.5,
            liquidity_access=0.5,
            manipulation=0.1,
            confidence=0.8,
            uncertainty=0.2,
            catalyst=0.3,
            exit_risk=0.4,
            research_priority=0.5,
            risk_band="high" if collapsed else "low",
            model_version="test",
        )
        session.add(score)
        session.flush()
        session.add(
            models.RiskOutcome(
                asset_id=asset.id,
                score_id=score.id,
                risk_band=score.risk_band,
                scored_at=decision_ts,
                lifecycle_phase_at_score="seeding",
                evaluated_at=decision_ts + timedelta(hours=25),
                collapsed=collapsed,
                rugged=False,
                survived=not collapsed,
            )
        )
        # Perfectly concordant with the outcome but observed an hour AFTER the
        # decision time: the exact point-in-time leak the audit hunts.
        session.add(
            models.Feature(
                asset_id=asset.id,
                decision_ts=decision_ts,
                observed_at=decision_ts + timedelta(hours=1),
                feature_name="future_peek",
                feature_value=1.0 if collapsed else 0.0,
                source_count=1,
                freshness_score=1.0,
                missing_flag=False,
            )
        )
    session.commit()

    report = run_harness(session, min_samples=5)
    cells = [
        cell
        for cell in report.cells
        if cell.output == "feature_leakage" and cell.metric == "suspected_features"
    ]
    assert cells, "run_harness must emit the feature_leakage/suspected_features cell"
    cell = cells[0]
    assert cell.n == n, "the audit must run over the eval outcome samples"
    assert cell.leakage_suspected is True
    leaked = [
        entry
        for entry in report.suspicious_results
        if entry.get("output") == "feature:future_peek"
    ]
    assert leaked, "the leaking feature must land in suspicious_results"
    assert "observed after" in leaked[0]["reason"]


def test_load_forecasts_uses_source_features_ts(session) -> None:
    """H25: the forecast's true decision point is details['source_features_ts']
    (when the features were actually computed), not the later training-run
    timestamp — outcome joins must use it, with a legacy fallback."""
    from datetime import UTC, datetime, timedelta

    from storage import models
    from storage.repository import upsert_asset
    from tests.conftest import seed_reference
    from validation.harness import load_forecasts

    chain, _source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Src111111111111111111111111111111111111111",
        symbol="SRC",
        name="Src Fixture",
        first_seen_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    run_ts = datetime(2026, 5, 3, tzinfo=UTC)
    features_ts = datetime(2026, 5, 2, 12, tzinfo=UTC)
    session.add(
        models.Forecast(
            asset_id=asset.id,
            decision_ts=run_ts,
            observed_at=run_ts,
            p_ignition_24h=0.1,
            p_collapse_24h=0.2,
            expected_hours_to_peak=None,
            expected_hours_to_collapse=None,
            details={"source_features_ts": features_ts.isoformat()},
            model_version="test-marked",
        )
    )
    # Legacy row without the marker falls back to decision_ts.
    session.add(
        models.Forecast(
            asset_id=asset.id,
            decision_ts=run_ts,
            observed_at=run_ts,
            p_ignition_24h=0.1,
            p_collapse_24h=0.2,
            expected_hours_to_peak=None,
            expected_hours_to_collapse=None,
            details={},
            model_version="test-legacy",
        )
    )
    session.commit()

    rows = load_forecasts(session)
    assert len(rows) == 2
    marked = [r for r in rows if r.source_features_ts == features_ts]
    assert len(marked) == 1
    legacy = [r for r in rows if r.source_features_ts == run_ts]
    assert len(legacy) == 1
