"""Tests for the phase-conditioned confidence calibration (v4, 2026-11-20).

Confidence is the calibrated survival probability for the (risk band,
lifecycle phase at score time) cell, fit out-of-sample on the v2_bounded
reference set. Evidence completeness is reported as uncertainty, not
confidence, so the most predictable rows (dead tokens) no longer look
maximally uncertain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.enums import RiskBand
from scoring.band_calibration import (
    FALLBACK_BAND_CONFIDENCE,
    phase_confidence_map,
    refresh_phase_confidence,
    set_band_confidence,
    set_phase_confidence,
)
from scoring.formulas import compute_scores
from storage import models

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
BASE = NOW - timedelta(days=30)  # scored 30d ago: inside any cutoff-48h window


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


def test_phase_map_fits_cell_survival(session) -> None:
    set_phase_confidence(None)
    _seed(session, 1, "BLACK", 5, total=20, survived=0)
    _seed(session, 2, "BLACK", 1, total=20, survived=19)

    summary = refresh_phase_confidence(session, cutoff=NOW, min_samples=10)

    assert summary["reference_rows"] == 40, summary
    mapping = phase_confidence_map()
    assert mapping is not None
    # 0/20 dead-phase BLACK -> clamped floor, not 0%; 19/20 ignition -> 95%.
    assert mapping[(RiskBand.BLACK, 5)] == 1.0
    assert mapping[(RiskBand.BLACK, 1)] == 95.0
    assert summary["cells"]["BLACK:5"]["level"] == "cell"
    assert summary["cells"]["BLACK:1"]["level"] == "cell"
    set_phase_confidence(None)


def test_phase_fallback_chain_cell_phase_band_table(session) -> None:
    set_phase_confidence(None)
    # (BLACK,1): 20 rows, 19 survived. (YELLOW,1): 3 rows, 3 survived —
    # below min_samples, so it falls to the phase-1 marginal (23 rows,
    # 22 survived). (BLACK,2): no rows at all — phase 2 has no marginal,
    # so it falls to the BLACK band marginal (40 rows, 39 survived).
    # (GREEN,0): nothing anywhere — the static fallback table.
    _seed(session, 1, "BLACK", 1, total=20, survived=19)
    _seed(session, 2, "YELLOW", 1, total=3, survived=3)
    _seed(session, 3, "BLACK", 3, total=20, survived=20)

    refresh_phase_confidence(session, cutoff=NOW, min_samples=10)
    mapping = phase_confidence_map()
    assert mapping is not None
    assert mapping[(RiskBand.YELLOW, 1)] == pytest.approx(100.0 * 22 / 23, abs=0.01)
    assert mapping[(RiskBand.BLACK, 2)] == pytest.approx(97.5, abs=0.01)
    assert mapping[(RiskBand.GREEN, 0)] == float(FALLBACK_BAND_CONFIDENCE[RiskBand.GREEN])
    set_phase_confidence(None)


def test_phase_refresh_ignores_post_cutoff_and_v1_rows(session) -> None:
    set_phase_confidence(None)
    _seed(session, 1, "BLACK", 5, total=10, survived=0)  # pre-cutoff v2
    _seed(
        session, 2, "BLACK", 5, total=10, survived=10, scored_at=NOW - timedelta(hours=1)
    )  # post-cutoff v2
    _seed(
        session, 3, "BLACK", 5, total=10, survived=10, label_version="v1_unbounded"
    )  # pre-cutoff v1

    summary = refresh_phase_confidence(session, cutoff=NOW, min_samples=10)

    assert summary["reference_rows"] == 10, summary
    mapping = phase_confidence_map()
    assert mapping is not None
    assert mapping[(RiskBand.BLACK, 5)] == 1.0
    set_phase_confidence(None)


def test_phase_refresh_excludes_ambiguous_outcomes(session) -> None:
    set_phase_confidence(None)
    _seed(session, 1, "BLACK", 5, total=10, survived=5)
    # 5 evaluated-but-ambiguous rows (neither survived nor collapsed).
    for index in range(5):
        ts = BASE - timedelta(hours=100 + index)
        session.add(
            models.RiskOutcome(
                asset_id=1,
                score_id=20000 + index,
                risk_band="BLACK",
                scored_at=ts,
                lifecycle_phase_at_score="dead",
                evaluated_at=NOW,
                collapsed=False,
                survived=False,
                rugged=False,
                label_version="v2_bounded",
            )
        )
        session.add(
            models.Feature(
                asset_id=1,
                decision_ts=ts,
                observed_at=ts,
                feature_name="lifecycle_phase",
                feature_value=5.0,
                missing_flag=False,
            )
        )
    session.commit()

    summary = refresh_phase_confidence(session, cutoff=NOW, min_samples=10)

    assert summary["reference_rows"] == 10, summary
    mapping = phase_confidence_map()
    assert mapping is not None
    assert mapping[(RiskBand.BLACK, 5)] == 50.0
    set_phase_confidence(None)


def test_compute_scores_uses_phase_map_when_installed() -> None:
    try:
        set_phase_confidence({(band, phase): 42.0 for band in RiskBand for phase in range(6)})
        result = compute_scores({"lifecycle_phase": 5.0}, [])
        assert result.confidence == 42.0
        assert result.confidence_source == "phase-calibrated"

        # No lifecycle_phase feature -> the band map applies instead.
        result = compute_scores({}, [])
        assert result.confidence_source != "phase-calibrated"
    finally:
        set_phase_confidence(None)


def test_compute_scores_falls_back_without_phase_map() -> None:
    set_phase_confidence(None)
    set_band_confidence(dict(FALLBACK_BAND_CONFIDENCE))
    try:
        result = compute_scores({"lifecycle_phase": 5.0}, [])
        assert result.confidence_source in ("fallback", "calibrated")
        assert result.confidence == FALLBACK_BAND_CONFIDENCE[result.risk_band]
    finally:
        set_band_confidence(None)


def test_dead_phase_rows_are_confident_and_not_uncertain() -> None:
    try:
        # Every band's dead phase survives ~never: confidence 1%, and the
        # row is the most predictable kind, so uncertainty must be low.
        # (The old formula gave uncertainty ~99 here via 100 - confidence.)
        set_phase_confidence({(band, 5): 1.0 for band in RiskBand})
        result = compute_scores({"lifecycle_phase": 5.0}, [])
        assert result.confidence == 1.0
        assert result.uncertainty < 60.0
    finally:
        set_phase_confidence(None)


def test_phase_map_cache_roundtrip() -> None:
    set_phase_confidence({(RiskBand.BLACK, 5): 1.0})
    assert phase_confidence_map() == {(RiskBand.BLACK, 5): 1.0}
    set_phase_confidence(None)
    assert phase_confidence_map() is None


def test_uncertainty_is_predictive_entropy() -> None:
    """v4b: uncertainty is the entropy of the calibrated survival probability
    (max at p=0.5, min near p=0/1), not evidence missingness."""
    try:
        set_phase_confidence({(band, phase): 50.0 for band in RiskBand for phase in range(6)})
        mid = compute_scores({"lifecycle_phase": 1.0}, [])
        assert mid.confidence == 50.0
        assert mid.uncertainty > 90.0
        set_phase_confidence({(band, phase): 99.0 for band in RiskBand for phase in range(6)})
        certain = compute_scores({"lifecycle_phase": 1.0}, [])
        assert certain.confidence == 99.0
        assert certain.uncertainty < 20.0
        assert mid.uncertainty > certain.uncertainty
    finally:
        set_phase_confidence(None)
