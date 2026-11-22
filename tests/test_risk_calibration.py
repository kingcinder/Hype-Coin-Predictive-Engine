"""Tests for risk-score calibration as a collapse probability (v6, 2026-11-20).

When a phase-confidence map is installed, the stored risk is the calibrated
collapse probability — the complement of the calibrated survival probability
— fit out-of-sample on the v2_bounded reference set. Without a phase map (or
without a lifecycle_phase feature) the raw rule-engine score is kept.

Why: the raw rule score is badly calibrated as a collapse probability
(mean |risk/100 - collapsed| = 0.5026 on the v5 eval set; within-phase it is
inverted — lower risk, higher collapse in phase 1), and its isotonic
recalibration generalises worse than the raw score (mean error 0.7085
out-of-sample). The phase-conditioned rate is the calibration.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.enums import RiskBand
from risk_engine.rules import assess_risk
from scoring.band_calibration import (
    phase_confidence_map,
    refresh_phase_confidence,
    set_phase_confidence,
)
from scoring.formulas import compute_scores
from storage import models

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
BASE = NOW - timedelta(days=30)  # scored 30d ago: inside any cutoff-48h window

# Low-liquidity feature set: hard-rejects to BLACK (from test_risk_scoring).
LOW_LIQUIDITY_FEATURES = {
    "five_min_return": 80.0,
    "one_hour_return": 150.0,
    "volume_acceleration": 10.0,
    "liquidity_depth": 1000.0,
    "liquidity_change": 25.0,
    "buy_sell_ratio": 4.0,
    "unique_buyers_estimate": 100.0,
    "pair_age_minutes": 5.0,
    "holder_count": 20.0,
    "holder_growth": 10.0,
    "top_holder_concentration": 0.1,
    "spread_estimate": 5.0,
    "volatility": 10.0,
    "venue_agreement": 100.0,
    "mention_velocity": 50.0,
    "website_presence": 1.0,
    "github_presence_public": 1.0,
    "suspicious_contract_flags": 0.0,
    "deployer_history_available": 0.0,
    "narrative_acceleration": 3.0,
}


def _seed(
    session,
    asset_id: int,
    band: str,
    phase: int,
    *,
    total: int,
    survived: int,
    scored_at: datetime = BASE,
    label_version: str = "v2_bounded",
) -> None:
    """Seed `total` resolved v2 outcomes in one (band, phase) cell, with the
    matching lifecycle_phase Feature rows the fit joins on."""
    for index in range(total):
        ts = scored_at - timedelta(hours=index + 1)
        session.add(
            models.RiskOutcome(
                asset_id=asset_id,
                score_id=10000 + asset_id * 1000 + index,
                risk_band=band,
                scored_at=ts,
                lifecycle_phase_at_score="dead" if phase == 5 else "ignition",
                evaluated_at=NOW,
                collapsed=index >= survived,
                survived=index < survived,
                rugged=False,
                label_version=label_version,
            )
        )
        session.add(
            models.Feature(
                asset_id=asset_id,
                decision_ts=ts,
                observed_at=ts,
                feature_name="lifecycle_phase",
                feature_value=float(phase),
                missing_flag=False,
            )
        )
    session.commit()


def test_risk_is_complement_of_phase_calibrated_confidence() -> None:
    """With a phase map installed, risk = 100 - confidence for the cell."""
    set_phase_confidence({(band, phase): 80.0 for band in RiskBand for phase in range(6)})
    try:
        features = dict(LOW_LIQUIDITY_FEATURES, lifecycle_phase=5.0)
        result = compute_scores(features, [])
        assert result.confidence_source == "phase-calibrated"
        assert result.confidence == pytest.approx(80.0)
        assert result.risk == pytest.approx(20.0)
        assert result.risk + result.confidence == pytest.approx(100.0)

        # A different phase with the same uniform map: same complement.
        features = dict(LOW_LIQUIDITY_FEATURES, lifecycle_phase=1.0)
        result = compute_scores(features, [])
        assert result.confidence == pytest.approx(80.0)
        assert result.risk == pytest.approx(20.0)
    finally:
        set_phase_confidence(None)


def test_risk_uses_fitted_cell_values(session) -> None:
    """Risk follows the fitted (band, phase) cell: dead phase -> ~99,
    ignition phase -> ~5, on the reference-fit map."""
    set_phase_confidence(None)
    _seed(session, 1, "BLACK", 5, total=20, survived=0)
    _seed(session, 2, "BLACK", 1, total=20, survived=19)
    refresh_phase_confidence(session, cutoff=NOW, min_samples=10)
    try:
        mapping = phase_confidence_map()
        assert mapping is not None
        assert mapping[(RiskBand.BLACK, 5)] == 1.0
        assert mapping[(RiskBand.BLACK, 1)] == 95.0

        dead = compute_scores(dict(LOW_LIQUIDITY_FEATURES, lifecycle_phase=5.0), [])
        assert dead.risk_band == RiskBand.BLACK
        assert dead.confidence == pytest.approx(1.0)
        assert dead.risk == pytest.approx(99.0)

        ignition = compute_scores(dict(LOW_LIQUIDITY_FEATURES, lifecycle_phase=1.0), [])
        assert ignition.risk_band == RiskBand.BLACK
        assert ignition.confidence == pytest.approx(95.0)
        assert ignition.risk == pytest.approx(5.0)
    finally:
        set_phase_confidence(None)


def test_risk_keeps_raw_rule_score_without_phase_map() -> None:
    """No phase map installed: the stored risk is the raw rule-engine score."""
    set_phase_confidence(None)
    features = dict(LOW_LIQUIDITY_FEATURES, lifecycle_phase=5.0)
    result = compute_scores(features, [])
    assert result.confidence_source != "phase-calibrated"
    expected = assess_risk(dict(features)).score
    assert result.risk == pytest.approx(round(expected, 4))


def test_risk_falls_back_when_phase_feature_missing() -> None:
    """Phase map installed but no lifecycle_phase feature: confidence falls
    back to the band map and risk stays the raw rule score."""
    set_phase_confidence({(band, phase): 80.0 for band in RiskBand for phase in range(6)})
    try:
        features = dict(LOW_LIQUIDITY_FEATURES)  # no lifecycle_phase key
        result = compute_scores(features, [])
        assert result.confidence_source != "phase-calibrated"
        expected = assess_risk(dict(features)).score
        assert result.risk == pytest.approx(round(expected, 4))
    finally:
        set_phase_confidence(None)


def test_risk_stays_in_range_at_extreme_cells() -> None:
    """Calibrated risk is clamped to [0, 100] at the map extremes."""
    set_phase_confidence({(band, phase): 1.0 for band in RiskBand for phase in range(6)})
    try:
        result = compute_scores(dict(LOW_LIQUIDITY_FEATURES, lifecycle_phase=5.0), [])
        assert result.risk == pytest.approx(99.0)
        assert 0.0 <= result.risk <= 100.0
    finally:
        set_phase_confidence(None)
    set_phase_confidence({(band, phase): 99.0 for band in RiskBand for phase in range(6)})
    try:
        result = compute_scores(dict(LOW_LIQUIDITY_FEATURES, lifecycle_phase=1.0), [])
        assert result.risk == pytest.approx(1.0)
        assert 0.0 <= result.risk <= 100.0
    finally:
        set_phase_confidence(None)
