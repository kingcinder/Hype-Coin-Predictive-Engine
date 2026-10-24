from __future__ import annotations

from datetime import UTC, datetime, timedelta

from risk_engine.calibrator import run_confidence_calibration
from risk_engine.confidence_calibration import (
    CalibrationSample,
    apply_isotonic_map,
    chronological_split,
    fit_isotonic_map,
    retune_risk_thresholds,
)


def _sample(
    scored_at: datetime,
    evaluated_at: datetime,
    confidence: float,
    survived: bool,
) -> CalibrationSample:
    return CalibrationSample(
        scored_at=scored_at,
        evaluated_at=evaluated_at,
        confidence=confidence,
        survived=survived,
        collapsed=not survived,
        risk_score=100.0 - confidence,
    )


def test_chronological_split_excludes_training_labels_not_known_at_cutoff() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        _sample(start + timedelta(days=i), start + timedelta(days=i + 1), 20 + i, i % 2 == 0)
        for i in range(8)
    ]

    train, validation, split_at = chronological_split(samples, validation_fraction=0.25)

    assert split_at == start + timedelta(days=6)
    assert train
    assert validation
    assert all(row.scored_at < split_at and row.evaluated_at <= split_at for row in train)
    assert all(row.scored_at >= split_at for row in validation)


def test_chronological_split_returns_empty_train_when_labels_arrive_after_window() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        _sample(start + timedelta(days=i), start + timedelta(days=30), 20 + i, i % 2 == 0)
        for i in range(10)
    ]

    train, validation, _ = chronological_split(samples, validation_fraction=0.2)

    assert train == []
    assert validation


def test_isotonic_map_is_monotone_and_respects_sample_floor() -> None:
    x = [float(i) for i in range(100)]
    y = [i >= 60 for i in range(100)]

    mapping = fit_isotonic_map(x, y, min_samples=100)

    assert mapping is not None
    assert len(mapping[0]) == len(mapping[1])
    assert all(left <= right for left, right in zip(mapping[1], mapping[1][1:], strict=False))
    assert apply_isotonic_map(20.0, *mapping) < apply_isotonic_map(80.0, *mapping)
    assert fit_isotonic_map(x[:99], y[:99], min_samples=100) is None


def test_band_thresholds_are_derived_from_training_risk_scores_only() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        CalibrationSample(
            scored_at=start + timedelta(days=i),
            evaluated_at=start + timedelta(days=i + 1),
            confidence=100 - i,
            survived=i < 50,
            collapsed=i >= 50,
            risk_score=float(i),
        )
        for i in range(100)
    ]

    yellow, orange, red = retune_risk_thresholds(samples)

    assert 0 <= yellow < orange < red <= 100


def test_calibration_persists_fallback_when_training_labels_arrive_too_late(session) -> None:
    from storage.models import RiskConfidenceCalibration

    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        _sample(start + timedelta(days=i), start + timedelta(days=30), 20 + i, i % 2 == 0)
        for i in range(40)
    ]

    result = run_confidence_calibration(
        session, version="test-late-labels", samples=samples, min_samples=30
    )

    saved = session.query(RiskConfidenceCalibration).one()
    assert result.status == "fallback_insufficient_time_split"
    assert not saved.active
    assert saved.sample_size == 32
    assert saved.validation_sample_size == 8
    assert saved.x_points == []


def test_calibration_activates_only_after_training_and_heldout_gate(session) -> None:
    from storage.models import RiskCalibration, RiskConfidenceCalibration

    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        CalibrationSample(
            scored_at=start + timedelta(days=i),
            evaluated_at=start + timedelta(days=i + 1),
            confidence=float(i),
            survived=i >= 75,
            collapsed=i < 75,
            risk_score=float(100 - i),
        )
        for i in range(150)
    ]

    result = run_confidence_calibration(
        session, version="test-valid-time-split", samples=samples, min_samples=100
    )

    saved = session.query(RiskConfidenceCalibration).one()
    assert result.status == "active"
    assert saved.active
    assert saved.sample_size >= 100
    assert saved.validation_sample_size >= 30
    assert saved.calibrated_ece is not None and saved.calibrated_ece <= 0.10
    assert (
        session.query(RiskCalibration).one().yellow_threshold
        < session.query(RiskCalibration).one().red_threshold
    )


def test_compute_scores_uses_only_an_active_persisted_confidence_map(session) -> None:
    from common.time import utc_now
    from scoring.formulas import compute_scores
    from storage.models import RiskConfidenceCalibration

    session.add(
        RiskConfidenceCalibration(
            version="test-active",
            status="active",
            trained_at=utc_now(),
            sample_size=100,
            validation_sample_size=50,
            x_points=[0.0, 1.0],
            y_points=[0.9, 1.0],
            active=True,
        )
    )
    session.flush()

    result = compute_scores({}, session=session)

    assert result.confidence >= 90.0
