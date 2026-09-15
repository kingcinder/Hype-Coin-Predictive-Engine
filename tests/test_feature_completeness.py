"""E1: feature-completeness audit — missing-feature tracking and propagation.

Verifies:
- ``risk_engine.rules._feature`` tracking mode records defaulted feature
  names without changing the returned value (existing behavior preserved).
- ``assess_risk()`` reports every feature that silently hit its default via
  ``RiskAssessment.missing_features``.
- ``scoring.formulas.compute_scores`` merges factory-flagged missing
  features with rule-engine-tracked defaults and applies a
  feature-coverage-weighted confidence penalty: a token with 50% missing
  features scores lower confidence than one with 10% missing.
"""

from __future__ import annotations

import pytest

from features.definitions import FEATURE_NAMES
from risk_engine.rules import RiskAssessment, _feature, assess_risk
from scoring.formulas import compute_scores


def _full_features() -> dict[str, float]:
    """A plausible, fully populated feature vector (GREEN-leaning token)."""
    base: dict[str, float] = {
        "five_min_return": 2.0,
        "one_hour_return": 5.0,
        "volume_acceleration": 1.5,
        "liquidity_depth": 250_000.0,
        "liquidity_change": 10.0,
        "buy_sell_ratio": 1.2,
        "unique_buyers_estimate": 400.0,
        "pair_age_minutes": 5_000.0,
        "holder_count": 3_000.0,
        "holder_growth": 2.0,
        "top_holder_concentration": 0.15,
        "spread_estimate": 2.0,
        "volatility": 12.0,
        "venue_agreement": 95.0,
        "mention_velocity": 20.0,
        "website_presence": 1.0,
        "github_presence_public": 1.0,
        "suspicious_contract_flags": 0.0,
        "deployer_history_available": 1.0,
        "narrative_acceleration": 2.0,
        "ignition_signal": 0.0,
        "liquidity_withdrawal_signal": 0.0,
        "lp_removal_signal": 0.0,
        "recidivism_score": 0.0,
        "prelaunch_priority": 0.0,
        "catalyst_proximity_hours": 72.0,
        "narrative_cluster_growth_7d": 1.0,
        "shill_channel_diversity": 5.0,
        "prelaunch_narrative_velocity": 0.0,
        "kol_velocity": 3.0,
        "github_star_velocity": 1.0,
        "hf_download_velocity": 10.0,
        "rpc_pool_health": 1.0,
        "collapse_probability_24h": 0.02,
        "lifecycle_phase": 1.0,
    }
    assert set(base) == set(FEATURE_NAMES)
    return base


# ---------------------------------------------------------------------------
# _feature tracking mode
# ---------------------------------------------------------------------------


def test_feature_tracking_does_not_change_returned_value() -> None:
    tracked: set[str] = set()
    # Present key: value returned, nothing tracked.
    assert _feature({"a": 3.5}, "a", defaulted=tracked) == 3.5
    assert _feature({"a": 3.5}, "a") == 3.5  # no tracker: same value
    assert tracked == set()
    # Missing key: default returned either way, tracked only with the set.
    assert _feature({}, "a", 1.0) == 1.0
    assert _feature({}, "a", 1.0, defaulted=tracked) == 1.0
    assert tracked == {"a"}


@pytest.mark.parametrize("bad", [None, "not-a-number", object()])
def test_feature_tracking_records_unparseable_values(bad: object) -> None:
    tracked: set[str] = set()
    assert _feature({"a": bad}, "a", 7.0, defaulted=tracked) == 7.0
    assert tracked == {"a"}


def test_feature_without_tracker_preserves_legacy_behavior() -> None:
    # Legacy callers that never pass ``defaulted`` see byte-identical values.
    assert _feature({}, "x") == 0.0
    assert _feature({"x": None}, "x", 2.0) == 2.0
    assert _feature({"x": "4"}, "x") == 4.0


# ---------------------------------------------------------------------------
# assess_risk reports defaulted features
# ---------------------------------------------------------------------------


def test_assess_risk_reports_defaulted_features() -> None:
    features = _full_features()
    for name in ("liquidity_depth", "holder_count", "collapse_probability_24h"):
        del features[name]
    assessment = assess_risk(features)
    assert set(assessment.missing_features) == {
        "liquidity_depth",
        "holder_count",
        "collapse_probability_24h",
    }
    assert assessment.missing_features == sorted(assessment.missing_features)


def test_assess_risk_empty_dict_reports_all_rule_inputs() -> None:
    assessment = assess_risk({})
    assert set(assessment.missing_features) == {
        "liquidity_depth",
        "suspicious_contract_flags",
        "top_holder_concentration",
        "pair_age_minutes",
        "spread_estimate",
        "buy_sell_ratio",
        "holder_count",
        "volatility",
        "liquidity_withdrawal_signal",
        "lp_removal_signal",
        "recidivism_score",
        "collapse_probability_24h",
        "lifecycle_phase",
    }


def test_assess_risk_full_dict_reports_nothing_missing() -> None:
    assessment = assess_risk(_full_features())
    assert assessment.missing_features == []


def test_assess_risk_tracking_does_not_change_band_or_score() -> None:
    # Tracking is observational: a sparse dict must score exactly as it did
    # before tracking existed (defaults still apply, band thresholds intact).
    sparse = {"liquidity_depth": 1_000.0, "holder_count": 10.0}
    assessment = assess_risk(sparse)
    assert assessment.band.value == "BLACK"  # shallow book + new-pair rule
    assert assessment.score >= 40.0  # hard-reject floor
    assert "liquidity_depth" not in assessment.missing_features
    assert "holder_count" not in assessment.missing_features
    assert "collapse_probability_24h" in assessment.missing_features


def test_masked_forecast_feature_is_reported_missing() -> None:
    # mask_unreliable_forecast pops collapse_probability_24h when the
    # calibration health is red; assess_risk must surface that gap instead
    # of silently scoring a 0% collapse probability.
    features = _full_features()
    features.pop("collapse_probability_24h")
    assessment = assess_risk(features)
    assert "collapse_probability_24h" in assessment.missing_features


def test_risk_assessment_defaults_to_empty_missing_list() -> None:
    from common.enums import RiskBand

    assessment = RiskAssessment(RiskBand.GREEN, 0.0)
    assert assessment.missing_features == []


# ---------------------------------------------------------------------------
# compute_scores: union + coverage-weighted confidence penalty
# ---------------------------------------------------------------------------


def test_missing_features_union_is_accurate() -> None:
    features = _full_features()
    features.pop("liquidity_depth")  # rule engine will hit its default
    result = compute_scores(features, ["holder_count"])  # factory-flagged
    assert result.missing_features == ["holder_count", "liquidity_depth"]


def test_confidence_decreases_with_missing_feature_count() -> None:
    features = _full_features()
    names = sorted(FEATURE_NAMES)
    few_missing = names[:4]  # ~11%
    many_missing = names[:18]  # ~51%
    high = compute_scores(features, few_missing)
    low = compute_scores(features, many_missing)
    assert low.confidence < high.confidence
    # The penalty must be material, not a rounding artifact.
    assert high.confidence - low.confidence > 10.0
    assert len(low.missing_features) == len(many_missing)
    assert len(high.missing_features) == len(few_missing)


def test_full_features_beat_empty_dict_on_confidence() -> None:
    full = compute_scores(_full_features(), [])
    empty = compute_scores({}, [])
    assert full.confidence > empty.confidence
    # Empty dict: all 13 rule-engine inputs defaulted.
    assert len(empty.missing_features) == 13
    assert full.missing_features == []


def test_uncertainty_rises_with_missing_features() -> None:
    features = _full_features()
    names = sorted(FEATURE_NAMES)
    high = compute_scores(features, names[:4])
    low = compute_scores(features, names[:18])
    assert low.uncertainty > high.uncertainty
