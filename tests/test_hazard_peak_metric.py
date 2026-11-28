from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

from forecast.engine import FORECAST_FEATURE_NAMES, ForecastEngine, ForecastModel
from forecast.hazard import DiscreteHazardModel, HazardFit
from storage.repository import upsert_asset, upsert_feature
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)

COLLAPSE_MEAN_HOURS = 20.0
PEAK_MEAN_HOURS = 3.0


def _confident_classifier() -> tuple[HistGradientBoostingClassifier, IsotonicRegression]:
    """A tiny real classifier + calibrator that fires (~1.0) on the exact
    feature row the seeded asset produces at predict time (rpc_pool_health at
    its 1.0 default, lifecycle_phase seeded to ignition) and ~0.0 on the
    all-default row."""
    n_features = len(FORECAST_FEATURE_NAMES)
    hi = np.zeros(n_features)
    hi[FORECAST_FEATURE_NAMES.index("rpc_pool_health")] = 1.0
    hi[FORECAST_FEATURE_NAMES.index("lifecycle_phase")] = 1.0
    lo = np.zeros(n_features)
    rng = np.random.default_rng(7)
    features = np.vstack([np.tile(hi, (20, 1)), np.tile(lo, (20, 1))])
    features = features + rng.normal(0.0, 0.01, features.shape)
    labels = np.array([1] * 20 + [0] * 20)
    classifier = HistGradientBoostingClassifier(max_iter=50, random_state=7).fit(features, labels)
    raw = classifier.predict_proba(features)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip").fit(raw, labels)
    return classifier, calibrator


def _seed_predictable_asset(session) -> int:  # noqa: ANN001
    chain, _source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="AddrPEAK11111111111111111111111111111111111",
        symbol="PEAK",
        name="PEAK",
        first_seen_at=T0,
    )
    upsert_feature(
        session,
        asset_id=asset.id,
        decision_ts=T0,
        feature_name="lifecycle_phase",
        feature_value=1.0,  # ignition phase
        source_count=1,
        freshness_score=1.0,
        missing_flag=False,
    )
    session.commit()
    return int(asset.id)


def test_peak_fit_exposes_mean_time_to_peak_alias() -> None:
    fit = DiscreteHazardModel(24).fit(
        times_hours=[2.0, 3.0, 4.0, 24.0],
        events=[True, True, True, False],
    )
    assert fit.mean_time_to_collapse is not None
    assert fit.mean_time_to_peak == fit.mean_time_to_collapse
    empty = HazardFit(mean_time_to_collapse=None, event_count=0, at_risk_count=0)
    assert empty.mean_time_to_peak is None


def test_peak_metric_uses_peak_fit_mean_not_collapse_fit_mean(session) -> None:  # noqa: ANN001
    # Collapses happen late (mean ~20h) and peaks happen early (mean ~3h).
    # The peak fits store their time-to-peak means in the generic fit's single
    # mean-time field; _predict must read the PEAK fit — through its
    # peak-named alias — for expected_hours_to_peak, never the collapse fit.
    collapse_fit = HazardFit(
        mean_time_to_collapse=COLLAPSE_MEAN_HOURS, event_count=5, at_risk_count=10
    )
    peak_fit = HazardFit(mean_time_to_collapse=PEAK_MEAN_HOURS, event_count=5, at_risk_count=10)
    assert peak_fit.mean_time_to_peak == pytest.approx(PEAK_MEAN_HOURS)

    asset_id = _seed_predictable_asset(session)
    classifier, calibrator = _confident_classifier()
    model = ForecastModel(
        ignition=classifier,
        collapse=classifier,
        ignition_calibrator=calibrator,
        collapse_calibrator=calibrator,
        hazard=collapse_fit,
        peak_hazard=peak_fit,
        hazards_by_phase={"ignition": collapse_fit},
        peak_hazards_by_phase={"ignition": peak_fit},
        calibrated=True,
    )
    engine = ForecastEngine()
    predicted = engine._predict(session, model, T0)
    session.commit()
    assert len(predicted) == 1
    forecast = predicted[0]
    assert forecast.asset_id == asset_id
    assert forecast.expected_hours_to_peak == pytest.approx(PEAK_MEAN_HOURS)
    assert forecast.expected_hours_to_collapse == pytest.approx(COLLAPSE_MEAN_HOURS)
