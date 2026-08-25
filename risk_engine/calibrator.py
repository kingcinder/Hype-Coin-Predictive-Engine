"""Risk Calibrator — learns optimal risk thresholds from historical outcomes.

Instead of hardcoded thresholds, this module analyzes what actually
happened to tokens flagged at each risk band and adjusts the score
thresholds accordingly.  Red bands with high false-positive rates get
relaxed; orange bands that miss collapses get tightened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.enums import RiskBand
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from risk_engine.outcomes import BandOutcome, RiskOutcomeReport, evaluate_outcomes
from storage import models

log = get_logger(__name__)

# Default thresholds (used as fallback when no calibration data exists)
DEFAULT_YELLOW = 25.0
DEFAULT_ORANGE = 50.0
DEFAULT_RED = 75.0

# Minimum samples before we trust calibration data
MIN_CALIBRATION_SAMPLES = 10

# Maximum adjustment per calibration cycle (prevent wild swings)
MAX_THRESHOLD_DRIFT = 15.0

# Learning rate: how much to move toward the ideal per cycle
LEARNING_RATE = 0.3


@dataclass
class CalibrationResult:
    """The result of a calibration pass."""

    version: str
    yellow_threshold: float
    orange_threshold: float
    red_threshold: float
    reason_weights: dict[str, float]
    band_precisions: dict[str, float]
    sample_size: int
    adjusted: bool


def _get_active_calibration(session: Session) -> models.RiskCalibration | None:
    """Get the current active calibration."""
    return session.scalar(
        select(models.RiskCalibration)
        .where(models.RiskCalibration.active.is_(True))
        .order_by(models.RiskCalibration.calibrated_at.desc())
        .limit(1)
    )


def get_current_thresholds(session: Session) -> tuple[float, float, float]:
    """Return the current (possibly calibrated) risk thresholds.

    Falls back to defaults if no calibration data exists.
    """
    cal = _get_active_calibration(session)
    if cal is None:
        return DEFAULT_YELLOW, DEFAULT_ORANGE, DEFAULT_RED
    return cal.yellow_threshold, cal.orange_threshold, cal.red_threshold


def get_reason_weights(session: Session) -> dict[str, float]:
    """Return the current adaptive reason weights.

    These weights adjust how much each risk reason contributes to the
    total risk score.  Learned from which reasons best predict actual
    collapses.
    """
    cal = _get_active_calibration(session)
    if cal is None:
        return {}
    return cal.reason_weights


def _ideal_thresholds_for_band(band: BandOutcome, current_threshold: float) -> float:
    """Compute the ideal threshold for a band based on its precision.

    A band with high precision (>0.7) is doing well — keep or tighten slightly.
    A band with low precision (<0.3) has too many false positives — relax it.
    """
    if band.total_flagged < MIN_CALIBRATION_SAMPLES:
        return current_threshold

    precision = band.precision

    if precision >= 0.7:
        # Good precision: tighten slightly (raise threshold) to be more selective
        ideal = current_threshold + (precision - 0.5) * 10.0
    elif precision >= 0.4:
        # Acceptable: small adjustment toward ideal
        ideal = current_threshold + (precision - 0.5) * 5.0
    else:
        # Poor precision: relax (lower threshold) to reduce false positives
        ideal = current_threshold - (0.5 - precision) * 15.0

    return max(0.0, min(100.0, ideal))


def _compute_reason_weights(
    session: Session,
    report: RiskOutcomeReport,
) -> dict[str, float]:
    """Analyze which risk reasons are most predictive of actual collapses.

    Batch-loads all explanations to avoid N+1 queries, then computes the
    relative frequency of each reason in collapsed vs survived tokens.
    """
    collapsed_outcomes = session.scalars(
        select(models.RiskOutcome).where(
            models.RiskOutcome.evaluated_at.is_not(None),
            models.RiskOutcome.collapsed.is_(True),
        )
    ).all()

    survived_outcomes = session.scalars(
        select(models.RiskOutcome).where(
            models.RiskOutcome.evaluated_at.is_not(None),
            models.RiskOutcome.survived.is_(True),
        )
    ).all()

    if not collapsed_outcomes:
        return {}

    # Batch-load all explanations by score_id to avoid N+1 queries
    all_score_ids = list(
        {o.score_id for o in collapsed_outcomes} | {o.score_id for o in survived_outcomes}
    )
    explanations = session.scalars(
        select(models.ScoreExplanation).where(models.ScoreExplanation.score_id.in_(all_score_ids))
    ).all()
    explanation_by_score = {e.score_id: e for e in explanations}

    # Get the risk reasons for collapsed and survived tokens via their scores
    collapsed_reasons: dict[str, int] = {}
    survived_reasons: dict[str, int] = {}

    for outcome in collapsed_outcomes:
        explanation = explanation_by_score.get(outcome.score_id)
        if explanation:
            for reason in explanation.risk_reasons:
                # Normalize reason to a category key
                key = reason.split(":")[0].strip()[:60]
                collapsed_reasons[key] = collapsed_reasons.get(key, 0) + 1

    for outcome in survived_outcomes:
        explanation = explanation_by_score.get(outcome.score_id)
        if explanation:
            for reason in explanation.risk_reasons:
                key = reason.split(":")[0].strip()[:60]
                survived_reasons[key] = survived_reasons.get(key, 0) + 1

    # Compute weights: reasons that appear more in collapsed vs survived
    # get higher weights
    weights: dict[str, float] = {}
    all_keys = set(collapsed_reasons.keys()) | set(survived_reasons.keys())

    total_collapsed = len(collapsed_outcomes) or 1
    total_survived = len(survived_outcomes) or 1

    for key in all_keys:
        collapsed_freq = collapsed_reasons.get(key, 0) / total_collapsed
        survived_freq = survived_reasons.get(key, 0) / total_survived
        # Weight = how much more likely a reason is in collapsed vs survived
        if survived_freq > 0:
            weight = collapsed_freq / survived_freq
        else:
            weight = 2.0 if collapsed_freq > 0 else 1.0
        weights[key] = round(max(0.5, min(3.0, weight)), 4)

    return weights


def run_calibration(
    session: Session,
    *,
    decision_ts: datetime | None = None,
) -> CalibrationResult:
    """Run a full risk calibration pass.

    1. Evaluate all pending outcomes
    2. Compute band-level precision metrics
    3. Adjust thresholds based on precision
    4. Compute adaptive reason weights
    5. Persist the new calibration

    Returns the calibration result for health recording.
    """
    decision_ts = ensure_utc(decision_ts or utc_now())
    version = f"adaptive-v1-{decision_ts.strftime('%Y%m%d%H%M')}"

    # Step 1: Evaluate outcomes
    report = evaluate_outcomes(session, decision_ts=decision_ts)

    # Get current thresholds
    yellow, orange, red = get_current_thresholds(session)

    # Step 2: Compute ideal thresholds based on band precision
    adjusted = False
    if report.total_flagged >= MIN_CALIBRATION_SAMPLES:
        yellow_band = report.bands.get(RiskBand.YELLOW.value)
        orange_band = report.bands.get(RiskBand.ORANGE.value)
        red_band = report.bands.get(RiskBand.RED.value)

        if yellow_band and yellow_band.total_flagged >= MIN_CALIBRATION_SAMPLES:
            ideal_yellow = _ideal_thresholds_for_band(yellow_band, yellow)
            new_yellow = yellow + LEARNING_RATE * (ideal_yellow - yellow)
            new_yellow = max(
                0.0,
                min(yellow + MAX_THRESHOLD_DRIFT, max(yellow - MAX_THRESHOLD_DRIFT, new_yellow)),
            )
            if abs(new_yellow - yellow) > 0.5:
                adjusted = True
                yellow = round(new_yellow, 2)

        if orange_band and orange_band.total_flagged >= MIN_CALIBRATION_SAMPLES:
            ideal_orange = _ideal_thresholds_for_band(orange_band, orange)
            new_orange = orange + LEARNING_RATE * (ideal_orange - orange)
            new_orange = max(
                yellow + 5.0,  # Must be above yellow
                min(
                    orange + MAX_THRESHOLD_DRIFT,
                    max(orange - MAX_THRESHOLD_DRIFT, new_orange),
                ),
            )
            if abs(new_orange - orange) > 0.5:
                adjusted = True
                orange = round(new_orange, 2)

        if red_band and red_band.total_flagged >= MIN_CALIBRATION_SAMPLES:
            ideal_red = _ideal_thresholds_for_band(red_band, red)
            new_red = red + LEARNING_RATE * (ideal_red - red)
            new_red = max(
                orange + 5.0,  # Must be above orange
                min(
                    red + MAX_THRESHOLD_DRIFT,
                    max(red - MAX_THRESHOLD_DRIFT, new_red),
                ),
            )
            if abs(new_red - red) > 0.5:
                adjusted = True
                red = round(new_red, 2)

    # Step 3: Compute reason weights
    reason_weights = _compute_reason_weights(session, report)

    # Step 4: Collect band precisions
    band_precisions = {band: outcome.precision for band, outcome in report.bands.items()}

    # Step 5: Persist calibration
    # Deactivate previous calibration
    prev = _get_active_calibration(session)
    if prev:
        prev.active = False

    cal = models.RiskCalibration(
        version=version,
        calibrated_at=decision_ts,
        sample_size=report.total_flagged,
        yellow_threshold=yellow,
        orange_threshold=orange,
        red_threshold=red,
        reason_weights=reason_weights,
        band_precisions=band_precisions,
        active=True,
    )
    session.add(cal)
    session.flush()

    # Record health
    from storage.repository import record_health

    state = "ok" if adjusted else "yellow"
    record_health(
        session,
        component="risk_calibration",
        state=state,
        message=(
            f"thresholds: Y={yellow:.1f} O={orange:.1f} R={red:.1f} | "
            f"sample_size={report.total_flagged} | "
            f"overall_precision={report.overall_precision:.3f} | "
            f"adjusted={adjusted}"
        ),
    )

    log.info(
        "risk_calibration_complete",
        version=version,
        yellow=yellow,
        orange=orange,
        red=red,
        sample_size=report.total_flagged,
        overall_precision=round(report.overall_precision, 4),
        adjusted=adjusted,
    )

    return CalibrationResult(
        version=version,
        yellow_threshold=yellow,
        orange_threshold=orange,
        red_threshold=red,
        reason_weights=reason_weights,
        band_precisions=band_precisions,
        sample_size=report.total_flagged,
        adjusted=adjusted,
    )
