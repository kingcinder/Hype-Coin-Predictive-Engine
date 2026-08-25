"""Tests for the adaptive LLM weight calibration system."""

from __future__ import annotations

from unittest.mock import patch

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
