"""Tests for the adaptive LLM weight calibration system."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from scoring.llm_calibration import CalibrationSnapshot, LLMCalibrator
from storage import models
from tests.conftest import seed_market_asset


class TestLLMCalibratorRecordPrediction:
    def test_records_prediction(self, session) -> None:
        asset = seed_market_asset(session)
        calibrator = LLMCalibrator()
        calibrator.record_prediction(
            session,
            asset_id=asset.id,
            score_id=None,
            model_name="qwen2.5:0.5b",
            hype_delta=2.0,
            risk_delta=-1.0,
            confidence_delta=1.5,
            llm_weight=0.10,
            base_hype=50.0,
            base_risk=25.0,
            base_confidence=60.0,
            final_hype=52.0,
            final_risk=24.0,
            final_confidence=61.5,
        )
        session.flush()
        records = session.scalars(select(models.LLMCalibrationRecord)).all()
        assert len(records) == 1
        assert records[0].asset_id == asset.id
        assert records[0].hype_delta == 2.0
        assert records[0].model_name == "qwen2.5:0.5b"

    def test_disabled_calibration_skips(self, session) -> None:
        asset = seed_market_asset(session)
        with patch("scoring.llm_calibration.get_settings") as mock_settings:
            mock_settings.return_value.llm_calibration_enabled = False
            calibrator = LLMCalibrator()
            calibrator.record_prediction(
                session,
                asset_id=asset.id,
                score_id=None,
                model_name="test",
                hype_delta=1.0,
                risk_delta=0.0,
                confidence_delta=0.0,
                llm_weight=0.10,
                base_hype=50.0,
                base_risk=25.0,
                base_confidence=60.0,
                final_hype=51.0,
                final_risk=25.0,
                final_confidence=60.0,
            )
        session.flush()
        records = session.scalars(select(models.LLMCalibrationRecord)).all()
        assert len(records) == 0


class TestLLMCalibratorGetWeight:
    def test_default_weight_when_no_state(self, session) -> None:
        calibrator = LLMCalibrator()
        weight = calibrator.get_weight(session)
        assert weight == 0.10  # default from config

    def test_returns_persisted_weight(self, session) -> None:
        state = models.LLMCalibrationState(
            current_weight=0.20,
            previous_weight=0.15,
        )
        session.add(state)
        session.flush()
        calibrator = LLMCalibrator()
        weight = calibrator.get_weight(session)
        assert weight == 0.20


class TestLLMCalibratorCalibrate:
    def test_insufficient_samples_keeps_weight(self, session) -> None:
        asset = seed_market_asset(session)
        calibrator = LLMCalibrator()
        # Add only 5 records (below min_samples=10)
        for i in range(5):
            calibrator.record_prediction(
                session,
                asset_id=asset.id,
                score_id=None,
                model_name="test",
                hype_delta=float(i),
                risk_delta=0.0,
                confidence_delta=0.0,
                llm_weight=0.10,
                base_hype=50.0,
                base_risk=25.0,
                base_confidence=60.0,
                final_hype=50.0 + float(i),
                final_risk=25.0,
                final_confidence=60.0,
            )
        session.flush()
        weight = calibrator.calibrate(session)
        assert weight == 0.10  # unchanged

    def test_creates_state_on_first_calibration(self, session) -> None:
        calibrator = LLMCalibrator()
        calibrator.calibrate(session)
        state = session.scalar(select(models.LLMCalibrationState).limit(1))
        assert state is not None
        assert state.current_weight == 0.10


class TestLLMCalibratorSnapshot:
    def test_snapshot_returns_defaults_when_no_state(self, session) -> None:
        calibrator = LLMCalibrator()
        snap = calibrator.get_snapshot(session)
        assert isinstance(snap, CalibrationSnapshot)
        assert snap.current_weight == 0.10
        assert snap.total_predictions == 0
        assert snap.weight_history == []

    def test_snapshot_returns_persisted_state(self, session) -> None:
        state = models.LLMCalibrationState(
            current_weight=0.15,
            previous_weight=0.10,
            total_predictions=100,
            total_improved=60,
            total_degraded=40,
            weight_history=[{"ts": "2026-01-01T00:00:00", "old_weight": 0.10, "new_weight": 0.15}],
        )
        session.add(state)
        session.flush()
        calibrator = LLMCalibrator()
        snap = calibrator.get_snapshot(session)
        assert snap.current_weight == 0.15
        assert snap.total_predictions == 100
        assert snap.improvement_rate == 0.6
        assert len(snap.weight_history) == 1


class TestLLMCalibratorBandDistance:
    def test_green_band_near_safe_token(self) -> None:
        dist = LLMCalibrator._band_distance("GREEN", collapsed=False)
        assert dist == 0

    def test_black_band_near_collapsed_token(self) -> None:
        dist = LLMCalibrator._band_distance("BLACK", collapsed=True)
        assert dist == 0

    def test_green_band_near_collapsed_token(self) -> None:
        dist = LLMCalibrator._band_distance("GREEN", collapsed=True)
        assert dist == 4


class TestLLMCalibrationRegression:
    def test_tiebreaker_is_strict_no_credit_for_noop(self) -> None:
        """M34: an unchanged LLM adjustment (final_risk == base_risk) is not an
        improvement — the tiebreak requires strictly closer to the truth."""
        assess = LLMCalibrator._assess_improvement_tiebreaker
        # No-op: identical risk, token collapsed — not improvement.
        assert assess(80.0, 80.0, True) is False
        assert assess(20.0, 20.0, False) is False
        # Strictly closer to the truth counts.
        assert assess(80.0, 95.0, True) is True
        assert assess(80.0, 60.0, True) is False
        assert assess(20.0, 5.0, False) is True

    def test_prune_actually_deletes_stale_records(self, session) -> None:
        """M35: pruning must execute a DELETE — stale calibration records are
        removed from the table, not merely selected."""
        from datetime import timedelta

        from sqlalchemy import func

        from common.config import get_settings

        asset = seed_market_asset(session)
        settings = get_settings()
        cutoff = datetime.now(UTC) - timedelta(hours=settings.llm_calibration_window_hours * 2)
        old = models.LLMCalibrationRecord(
            asset_id=asset.id,
            prediction_ts=cutoff - timedelta(hours=1),
            model_name="test",
            hype_delta=0.0,
            risk_delta=0.0,
            confidence_delta=0.0,
            llm_weight_at_time=0.5,
            base_hype=0.5,
            base_risk=50.0,
            base_confidence=0.8,
            final_hype=0.5,
            final_risk=50.0,
            final_confidence=0.8,
            created_at=cutoff - timedelta(hours=1),
        )
        fresh = models.LLMCalibrationRecord(
            asset_id=asset.id,
            prediction_ts=datetime.now(UTC),
            model_name="test",
            hype_delta=0.0,
            risk_delta=0.0,
            confidence_delta=0.0,
            llm_weight_at_time=0.5,
            base_hype=0.5,
            base_risk=50.0,
            base_confidence=0.8,
            final_hype=0.5,
            final_risk=50.0,
            final_confidence=0.8,
            created_at=datetime.now(UTC),
        )
        session.add_all([old, fresh])
        session.commit()
        assert session.scalar(select(func.count()).select_from(models.LLMCalibrationRecord)) == 2

        LLMCalibrator().calibrate(session)
        session.commit()

        remaining = session.scalars(select(models.LLMCalibrationRecord)).all()
        assert len(remaining) == 1
        assert remaining[0].id == fresh.id


def test_calibration_evaluates_prediction_and_records_outcome(session, monkeypatch) -> None:
    from datetime import timedelta

    from common.time import utc_now

    asset = seed_market_asset(session)
    prediction = models.LLMCalibrationRecord(
        asset_id=asset.id,
        prediction_ts=utc_now() - timedelta(hours=2),
        model_name="test",
        hype_delta=1.0,
        risk_delta=5.0,
        confidence_delta=0.0,
        llm_weight_at_time=0.1,
        base_hype=40.0,
        base_risk=20.0,
        base_confidence=70.0,
        final_hype=41.0,
        final_risk=90.0,
        final_confidence=70.0,
    )
    session.add(prediction)
    session.flush()
    monkeypatch.setattr(
        "scoring.llm_calibration.get_settings",
        lambda: SimpleNamespace(llm_calibration_enabled=True, llm_calibration_eval_hours=1),
    )
    monkeypatch.setattr(
        LLMCalibrator,
        "_find_outcome",
        lambda _self, _session, _record: SimpleNamespace(
            collapsed=True, price_change_pct=-85.0, risk_band="BLACK"
        ),
    )

    evaluated = LLMCalibrator().evaluate_predictions(session)

    assert evaluated == 1
    assert prediction.evaluated_at is not None
    assert prediction.actual_risk_band == "BLACK"
    assert prediction.price_change_pct == -85.0
    assert prediction.collapsed is True
    assert prediction.llm_improved is True
    assert prediction.evaluation_details["actual_collapsed"] is True


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0.0, "GREEN"), (20.0, "YELLOW"), (40.0, "ORANGE"), (60.0, "RED"), (80.0, "BLACK")],
)
def test_risk_band_boundaries_match_scoring_contract(score: float, expected: str) -> None:
    assert LLMCalibrator._score_to_band(score) == expected


def test_calibration_caps_weight_and_persists_improvement_summary(session, monkeypatch) -> None:
    from common.time import utc_now

    asset = seed_market_asset(session)
    settings = SimpleNamespace(
        llm_calibration_enabled=True,
        llm_calibration_eval_hours=24,
        llm_calibration_window_hours=24,
        llm_calibration_min_samples=2,
        llm_calibration_ceiling=0.5,
        llm_calibration_floor=0.05,
        llm_calibration_step=0.1,
        llm_weight=0.1,
    )
    monkeypatch.setattr("scoring.llm_calibration.get_settings", lambda: settings)
    state = models.LLMCalibrationState(current_weight=0.45, previous_weight=0.4)
    session.add(state)
    session.add_all(
        [
            models.LLMCalibrationRecord(
                asset_id=asset.id,
                prediction_ts=utc_now(),
                model_name="test",
                hype_delta=0.0,
                risk_delta=0.0,
                confidence_delta=0.0,
                llm_weight_at_time=0.4,
                base_hype=50.0,
                base_risk=50.0,
                base_confidence=50.0,
                final_hype=50.0,
                final_risk=50.0,
                final_confidence=50.0,
                evaluated_at=utc_now(),
                llm_improved=True,
            )
            for _ in range(2)
        ]
    )
    session.flush()

    weight = LLMCalibrator().calibrate(session)

    assert weight == 0.5
    assert state.previous_weight == 0.45
    assert state.total_predictions == 2
    assert state.total_improved == 2
    assert state.total_degraded == 0
    assert state.weight_history[-1]["direction"] == "increased"
    assert state.weight_history[-1]["new_weight"] == 0.5


def test_calibration_decreases_to_floor_and_leaves_ties_unchanged(session, monkeypatch) -> None:
    from common.time import utc_now

    asset = seed_market_asset(session)
    settings = SimpleNamespace(
        llm_calibration_enabled=True,
        llm_calibration_eval_hours=24,
        llm_calibration_window_hours=24,
        llm_calibration_min_samples=2,
        llm_calibration_ceiling=0.5,
        llm_calibration_floor=0.05,
        llm_calibration_step=0.1,
        llm_weight=0.1,
    )
    monkeypatch.setattr("scoring.llm_calibration.get_settings", lambda: settings)
    state = models.LLMCalibrationState(current_weight=0.05, previous_weight=0.1)
    session.add(state)
    session.add_all(
        [
            models.LLMCalibrationRecord(
                asset_id=asset.id,
                prediction_ts=utc_now(),
                model_name="test",
                hype_delta=0.0,
                risk_delta=0.0,
                confidence_delta=0.0,
                llm_weight_at_time=0.05,
                base_hype=50.0,
                base_risk=50.0,
                base_confidence=50.0,
                final_hype=50.0,
                final_risk=50.0,
                final_confidence=50.0,
                evaluated_at=utc_now(),
                llm_improved=False,
            )
            for _ in range(2)
        ]
    )
    session.flush()

    session.commit()
    session.expunge_all()
    assert LLMCalibrator().calibrate(session) == 0.05
    refreshed = session.scalar(select(models.LLMCalibrationState).limit(1))
    assert refreshed is not None
    assert refreshed.weight_history[-1]["direction"] == "decreased"


def test_balanced_improvement_rate_leaves_calibrated_weight_unchanged(session, monkeypatch) -> None:
    from common.time import utc_now

    asset = seed_market_asset(session)
    settings = SimpleNamespace(
        llm_calibration_enabled=True,
        llm_calibration_eval_hours=24,
        llm_calibration_window_hours=24,
        llm_calibration_min_samples=2,
        llm_calibration_ceiling=0.5,
        llm_calibration_floor=0.05,
        llm_calibration_step=0.1,
        llm_weight=0.1,
    )
    monkeypatch.setattr("scoring.llm_calibration.get_settings", lambda: settings)
    state = models.LLMCalibrationState(current_weight=0.2, previous_weight=0.2)
    session.add(state)
    session.add_all(
        [
            models.LLMCalibrationRecord(
                asset_id=asset.id,
                prediction_ts=utc_now(),
                model_name="test",
                hype_delta=0.0,
                risk_delta=0.0,
                confidence_delta=0.0,
                llm_weight_at_time=0.2,
                base_hype=50.0,
                base_risk=50.0,
                base_confidence=50.0,
                final_hype=50.0,
                final_risk=50.0,
                final_confidence=50.0,
                evaluated_at=utc_now(),
                llm_improved=improved,
            )
            for improved in (True, False)
        ]
    )
    session.flush()
    session.commit()
    session.expunge_all()

    assert LLMCalibrator().calibrate(session) == 0.2
    refreshed = session.scalar(select(models.LLMCalibrationState).limit(1))
    assert refreshed is not None
    assert refreshed.weight_history[-1]["direction"] == "unchanged"
