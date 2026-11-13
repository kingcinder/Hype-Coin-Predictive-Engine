"""Tests for P2: band→survival confidence is measured, not asserted.

``_BAND_CONFIDENCE`` was a hardcoded table nothing recomputed or checked. The
calibration now derives survival per risk band from persisted, evaluated
``RiskOutcome`` rows, publishes it only when every band has enough samples and
survival is monotone, and reports validation status to the API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from api.schemas import score_family_validation
from common.enums import RiskBand
from scoring.band_calibration import (
    COMPONENT,
    FALLBACK_BAND_CONFIDENCE,
    band_confidence_map,
    refresh_band_calibration,
    set_band_confidence,
)
from storage import models

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _outcomes(session, band: str, *, total: int, survived: int) -> None:
    for index in range(total):
        session.add(
            models.RiskOutcome(
                asset_id=1,
                score_id=len(session.new) + 1,
                risk_band=band,
                scored_at=NOW - timedelta(hours=index + 1),
                lifecycle_phase_at_score="launch",
                evaluated_at=NOW,
                collapsed=index >= survived,
                survived=index < survived,
                rugged=False,
            )
        )


def test_defaults_are_reported_until_outcomes_validate(session) -> None:
    set_band_confidence(None)
    assert band_confidence_map() == FALLBACK_BAND_CONFIDENCE

    summary = refresh_band_calibration(session, min_samples=3)
    assert summary["validated"] is False
    assert band_confidence_map() == FALLBACK_BAND_CONFIDENCE
    # Unvalidated is visible, not implied: the API reads the persisted row.
    assert score_family_validation(session)["hype"].validated is False


def test_validated_calibration_comes_from_the_outcomes(session) -> None:
    set_band_confidence(None)
    # Monotone survival across the band order, each band above min samples.
    _outcomes(session, "GREEN", total=10, survived=9)
    _outcomes(session, "YELLOW", total=10, survived=7)
    _outcomes(session, "ORANGE", total=10, survived=5)
    _outcomes(session, "RED", total=10, survived=2)
    _outcomes(session, "BLACK", total=10, survived=1)
    session.commit()

    summary = refresh_band_calibration(session, min_samples=5)

    assert summary["validated"] is True, summary
    calibrated = band_confidence_map()
    assert calibrated[RiskBand.GREEN] == 90.0
    assert calibrated[RiskBand.BLACK] == 10.0
    assert calibrated[RiskBand.GREEN] > calibrated[RiskBand.BLACK]
    assert score_family_validation(session)["hype"].validated is True
    row = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == COMPONENT)
        .order_by(models.SystemHealth.ts.desc())
    )
    assert row is not None and "validated=True" in (row.message or "")


def test_non_monotone_survival_is_not_validated(session) -> None:
    set_band_confidence(None)
    # BLACK survives more often than GREEN: confidence no longer ranks risk.
    _outcomes(session, "GREEN", total=10, survived=2)
    _outcomes(session, "YELLOW", total=10, survived=4)
    _outcomes(session, "ORANGE", total=10, survived=5)
    _outcomes(session, "RED", total=10, survived=7)
    _outcomes(session, "BLACK", total=10, survived=9)
    session.commit()

    summary = refresh_band_calibration(session, min_samples=5)

    assert summary["validated"] is False
    assert band_confidence_map() == FALLBACK_BAND_CONFIDENCE
    set_band_confidence(None)


def test_unevaluated_outcomes_do_not_count(session) -> None:
    set_band_confidence(None)
    session.add(
        models.RiskOutcome(
            asset_id=1,
            score_id=9001,
            risk_band="GREEN",
            scored_at=NOW,
            lifecycle_phase_at_score="launch",
            evaluated_at=None,
            collapsed=False,
            survived=True,
            rugged=False,
        )
    )
    session.commit()

    summary = refresh_band_calibration(session, min_samples=1)

    assert summary["validated"] is False
    assert summary["samples"] == {}
