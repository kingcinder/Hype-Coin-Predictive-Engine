"""Risk Calibrator — learns optimal risk thresholds from historical outcomes.

Instead of hardcoded thresholds, this module analyzes what actually
happened to tokens flagged at each risk band and adjusts the score
thresholds accordingly.  Red bands with high false-positive rates get
relaxed; orange bands that miss collapses get tightened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.enums import RiskBand
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from risk_engine.confidence_calibration import (
    MIN_VALIDATION_SAMPLES,
    CalibrationSample,
    apply_isotonic_map,
    chronological_split,
    fit_isotonic_map,
    retune_risk_thresholds,
)
from risk_engine.outcomes import BandOutcome, RiskOutcomeReport, evaluate_outcomes
from storage import models
from validation.metrics import expected_calibration_error

log = get_logger(__name__)

# Default thresholds (used as fallback when no calibration data exists)
DEFAULT_YELLOW = 25.0
DEFAULT_ORANGE = 50.0
DEFAULT_RED = 75.0

# Default ML-specific collapse-probability thresholds (0.0-1.0 scale),
# learned directly from ML scorer outcomes instead of bridged from the
# rule-engine score thresholds.
DEFAULT_ML_YELLOW = 0.10
DEFAULT_ML_ORANGE = 0.30
DEFAULT_ML_RED = 0.50

# Minimum samples before we trust calibration data
MIN_CALIBRATION_SAMPLES = 10

# Maximum adjustment per calibration cycle (prevent wild swings)
MAX_THRESHOLD_DRIFT = 15.0

# Same drift cap for the ML probability thresholds (0-1 scale)
MAX_ML_THRESHOLD_DRIFT = 0.15

# Hard ceiling for the ML red threshold.  The band mapper checks BLACK
# (0.75) before RED, so a calibrated red >= 0.75 would make the RED band
# unreachable.  Keeping red <= 0.70 preserves a guaranteed RED range below
# BLACK (mirrors the old score-bridge cap of 0.70).
MAX_ML_RED_THRESHOLD = 0.70

# Learning rate: how much to move toward the ideal per cycle
LEARNING_RATE = 0.3


@dataclass(frozen=True)
class ConfidenceCalibrationResult:
    version: str
    status: str
    sample_size: int
    validation_sample_size: int
    baseline_ece: float | None
    calibrated_ece: float | None
    training_started_at: datetime | None
    training_ended_at: datetime | None
    validation_started_at: datetime | None
    thresholds: tuple[float, float, float] | None


def _confidence_calibration_samples(session: Session) -> list[CalibrationSample]:
    """Read only completed, unambiguous labels joined to their scored values."""
    rows = session.execute(
        select(
            models.RiskOutcome.scored_at,
            models.RiskOutcome.evaluated_at,
            models.RiskOutcome.collapsed,
            models.RiskOutcome.survived,
            models.Score.confidence,
            models.Score.risk,
        )
        .join(models.Score, models.Score.id == models.RiskOutcome.score_id)
        .where(
            models.RiskOutcome.evaluated_at.is_not(None),
            models.RiskOutcome.collapsed != models.RiskOutcome.survived,
            models.RiskOutcome.scored_at <= models.RiskOutcome.evaluated_at,
        )
        .order_by(models.RiskOutcome.scored_at.asc())
    ).all()
    return [
        CalibrationSample(
            scored_at=row.scored_at,
            evaluated_at=row.evaluated_at,
            confidence=float(row.confidence),
            survived=bool(row.survived),
            collapsed=bool(row.collapsed),
            risk_score=float(row.risk),
        )
        for row in rows
        if row.evaluated_at is not None
    ]


def run_confidence_calibration(
    session: Session,
    *,
    version: str,
    samples: list[CalibrationSample] | None = None,
    min_samples: int | None = None,
    trained_at: datetime | None = None,
) -> ConfidenceCalibrationResult:
    """Fit and persist a chronological confidence map; keep fallback on weak evidence."""
    from common.config import get_settings

    sample_floor = min_samples or get_settings().risk_confidence_calibration_min_samples
    fit_rows = samples if samples is not None else _confidence_calibration_samples(session)
    train, validation, split_at = chronological_split(fit_rows)
    trained_at = ensure_utc(trained_at or utc_now())
    x_points: list[float] = []
    y_points: list[float] = []
    baseline_ece: float | None = None
    calibrated_ece: float | None = None
    thresholds: tuple[float, float, float] | None = None
    status = "fallback_insufficient_time_split"

    if len(train) >= sample_floor and len(validation) >= MIN_VALIDATION_SAMPLES:
        mapping = fit_isotonic_map(
            [row.confidence for row in train],
            [row.survived for row in train],
            min_samples=sample_floor,
        )
        if mapping is not None:
            x_points, y_points = mapping
            probabilities = np.asarray([row.confidence / 100.0 for row in validation])
            labels = np.asarray([row.survived for row in validation], dtype=float)
            calibrated = np.asarray(
                [
                    apply_isotonic_map(row.confidence, x_points, y_points) / 100.0
                    for row in validation
                ]
            )
            baseline_ece = expected_calibration_error(probabilities, labels)
            calibrated_ece = expected_calibration_error(calibrated, labels)
            status = "active" if calibrated_ece <= 0.10 else "fallback_above_ece_ceiling"
            thresholds = retune_risk_thresholds(train)
        else:
            status = "fallback_fit_unavailable"

    active = status == "active"
    if active:
        previous = session.scalars(
            select(models.RiskConfidenceCalibration).where(
                models.RiskConfidenceCalibration.active.is_(True)
            )
        ).all()
        for row in previous:
            row.active = False

    model_row = models.RiskConfidenceCalibration(
        version=version,
        status=status,
        trained_at=trained_at,
        training_started_at=min((ensure_utc(row.scored_at) for row in train), default=None),
        training_ended_at=split_at,
        validation_started_at=split_at,
        sample_size=len(train),
        validation_sample_size=len(validation),
        baseline_ece=baseline_ece,
        calibrated_ece=calibrated_ece,
        x_points=x_points,
        y_points=y_points,
        yellow_threshold=thresholds[0] if active and thresholds else None,
        orange_threshold=thresholds[1] if active and thresholds else None,
        red_threshold=thresholds[2] if active and thresholds else None,
        active=active,
    )
    session.add(model_row)

    if active and thresholds:
        previous_thresholds = _get_active_calibration(session)
        if previous_thresholds:
            previous_thresholds.active = False
        band_precisions = previous_thresholds.band_precisions if previous_thresholds else {}
        ml_yellow = (
            previous_thresholds.ml_yellow_threshold if previous_thresholds else DEFAULT_ML_YELLOW
        )
        ml_orange = (
            previous_thresholds.ml_orange_threshold if previous_thresholds else DEFAULT_ML_ORANGE
        )
        ml_red = previous_thresholds.ml_red_threshold if previous_thresholds else DEFAULT_ML_RED
        ml_band_precisions = previous_thresholds.ml_band_precisions if previous_thresholds else {}
        session.add(
            models.RiskCalibration(
                version=version,
                calibrated_at=trained_at,
                sample_size=len(train),
                yellow_threshold=thresholds[0],
                orange_threshold=thresholds[1],
                red_threshold=thresholds[2],
                reason_weights=previous_thresholds.reason_weights if previous_thresholds else {},
                band_precisions=band_precisions,
                ml_yellow_threshold=ml_yellow,
                ml_orange_threshold=ml_orange,
                ml_red_threshold=ml_red,
                ml_band_precisions=ml_band_precisions,
                active=True,
            )
        )

    from storage.repository import record_health

    record_health(
        session,
        component="risk_confidence_calibration",
        state="ok" if active else "yellow",
        message=(
            f"status={status}; train={len(train)}; validation={len(validation)}; "
            f"baseline_ece={baseline_ece}; calibrated_ece={calibrated_ece}"
        ),
    )
    session.flush()
    session.info.pop("risk_active_confidence_calibration", None)
    return ConfidenceCalibrationResult(
        version=version,
        status=status,
        sample_size=len(train),
        validation_sample_size=len(validation),
        baseline_ece=baseline_ece,
        calibrated_ece=calibrated_ece,
        training_started_at=model_row.training_started_at,
        training_ended_at=split_at,
        validation_started_at=split_at,
        thresholds=thresholds if active else None,
    )


def get_active_confidence_calibration(
    session: Session,
) -> models.RiskConfidenceCalibration | None:
    """Return the currently applied model, if the held-out ECE gate passed."""
    cache_key = "risk_active_confidence_calibration"
    if cache_key in session.info:
        return session.info[cache_key]
    active = session.scalar(
        select(models.RiskConfidenceCalibration)
        .where(models.RiskConfidenceCalibration.active.is_(True))
        .order_by(models.RiskConfidenceCalibration.trained_at.desc())
        .limit(1)
    )
    session.info[cache_key] = active
    return active


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
    # ML-specific probability thresholds (0.0-1.0), learned from ML outcomes
    ml_yellow_threshold: float = DEFAULT_ML_YELLOW
    ml_orange_threshold: float = DEFAULT_ML_ORANGE
    ml_red_threshold: float = DEFAULT_ML_RED
    ml_band_precisions: dict[str, float] = field(default_factory=dict)
    ml_adjusted: bool = False


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


def get_current_ml_thresholds(session: Session) -> tuple[float, float, float]:
    """Return the current ML-specific collapse-probability thresholds.

    These are on the 0.0–1.0 probability scale and are learned directly from
    ML scorer outcomes (RiskOutcome.details["ml_risk_band"]).  Falls back to
    the hardcoded probability defaults when no calibration exists yet.
    """
    cal = _get_active_calibration(session)
    if cal is None:
        return DEFAULT_ML_YELLOW, DEFAULT_ML_ORANGE, DEFAULT_ML_RED
    return (
        cal.ml_yellow_threshold,
        cal.ml_orange_threshold,
        cal.ml_red_threshold,
    )


def _ideal_thresholds_for_band(
    band: BandOutcome,
    current_threshold: float,
    *,
    scale_max: float = 100.0,
    step: float = 10.0,
) -> float:
    """Compute the ideal threshold for a band based on its precision.

    A band with high precision (>0.7) is doing well — keep or tighten slightly.
    A band with low precision (<0.4) has too many false positives — tighten it
    (raise the threshold) so it flags fewer, more selective candidates.

    ``scale_max`` bounds the result (100.0 for the rule score scale, 1.0 for
    the ML probability scale) and ``step`` scales the adjustment magnitude
    (10.0 score points vs 0.10 probability points), so the same precision
    heuristic drives both the rule and ML threshold updates.
    """
    if band.total_flagged < MIN_CALIBRATION_SAMPLES:
        return current_threshold

    precision = band.precision

    if precision >= 0.7:
        # Good precision: tighten slightly (raise threshold) to be more selective
        ideal = current_threshold + (precision - 0.5) * step
    elif precision >= 0.4:
        # Acceptable: small adjustment toward ideal
        ideal = current_threshold + (precision - 0.5) * step * 0.5
    else:
        # Poor precision: tighten (raise threshold) to cut false positives.
        # Lowering the threshold here would flag MORE candidates and make the
        # false-positive problem worse, not better.
        ideal = current_threshold + (0.5 - precision) * step * 1.5

    return max(0.0, min(scale_max, ideal))


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

    # Step 2b: Adjust ML-specific probability thresholds from ML outcomes.
    # The ML scorer calibrates on its own signal (RiskOutcome.details
    # ["ml_risk_band"]), so it is not bridged from the rule-engine score
    # thresholds above.
    ml_yellow, ml_orange, ml_red = get_current_ml_thresholds(session)
    ml_adjusted = False
    if report.ml_total_flagged >= MIN_CALIBRATION_SAMPLES:
        ml_yellow_band = report.ml_bands.get(RiskBand.YELLOW.value)
        ml_orange_band = report.ml_bands.get(RiskBand.ORANGE.value)
        ml_red_band = report.ml_bands.get(RiskBand.RED.value)

        if ml_yellow_band and ml_yellow_band.total_flagged >= MIN_CALIBRATION_SAMPLES:
            ideal = _ideal_thresholds_for_band(ml_yellow_band, ml_yellow, scale_max=1.0, step=0.10)
            new = ml_yellow + LEARNING_RATE * (ideal - ml_yellow)
            new = max(
                0.0,
                min(
                    ml_yellow + MAX_ML_THRESHOLD_DRIFT,
                    max(ml_yellow - MAX_ML_THRESHOLD_DRIFT, new),
                ),
            )
            # Keep yellow strictly below orange's ceiling so the ordering
            # gaps (0.05) always hold once red is capped at 0.70.
            new = min(new, MAX_ML_RED_THRESHOLD - 0.10)
            if abs(new - ml_yellow) > 0.005:
                ml_adjusted = True
                ml_yellow = round(new, 4)

        if ml_orange_band and ml_orange_band.total_flagged >= MIN_CALIBRATION_SAMPLES:
            ideal = _ideal_thresholds_for_band(ml_orange_band, ml_orange, scale_max=1.0, step=0.10)
            new = ml_orange + LEARNING_RATE * (ideal - ml_orange)
            new = max(
                ml_yellow + 0.05,  # must stay above yellow
                min(
                    ml_orange + MAX_ML_THRESHOLD_DRIFT,
                    max(ml_orange - MAX_ML_THRESHOLD_DRIFT, new),
                ),
            )
            # Never allow orange to drift into red's reserved range so the
            # red > orange + 0.05 gap survives the 0.70 red cap.
            new = min(new, MAX_ML_RED_THRESHOLD - 0.05)
            if abs(new - ml_orange) > 0.005:
                ml_adjusted = True
                ml_orange = round(new, 4)

        if ml_red_band and ml_red_band.total_flagged >= MIN_CALIBRATION_SAMPLES:
            ideal = _ideal_thresholds_for_band(ml_red_band, ml_red, scale_max=1.0, step=0.10)
            new = ml_red + LEARNING_RATE * (ideal - ml_red)
            new = max(
                ml_orange + 0.05,  # must stay above orange
                min(
                    ml_red + MAX_ML_THRESHOLD_DRIFT,
                    max(ml_red - MAX_ML_THRESHOLD_DRIFT, new),
                ),
            )
            # Never allow the ML red boundary to swallow BLACK (checked first
            # in band_from_collapse_probability).
            new = min(new, MAX_ML_RED_THRESHOLD)
            if abs(new - ml_red) > 0.005:
                ml_adjusted = True
                ml_red = round(new, 4)

    # Step 3: Compute reason weights
    reason_weights = _compute_reason_weights(session, report)

    # Step 4: Collect band precisions
    band_precisions = {band: outcome.precision for band, outcome in report.bands.items()}
    ml_band_precisions = {band: outcome.precision for band, outcome in report.ml_bands.items()}

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
        ml_yellow_threshold=ml_yellow,
        ml_orange_threshold=ml_orange,
        ml_red_threshold=ml_red,
        reason_weights=reason_weights,
        band_precisions=band_precisions,
        ml_band_precisions=ml_band_precisions,
        active=True,
    )
    session.add(cal)
    session.flush()

    # Record health
    from storage.repository import record_health

    state = "ok" if (adjusted or ml_adjusted) else "yellow"
    record_health(
        session,
        component="risk_calibration",
        state=state,
        message=(
            f"thresholds: Y={yellow:.1f} O={orange:.1f} R={red:.1f} | "
            f"ml: Y={ml_yellow:.3f} O={ml_orange:.3f} R={ml_red:.3f} | "
            f"sample_size={report.total_flagged} | "
            f"overall_precision={report.overall_precision:.3f} | "
            f"adjusted={adjusted} ml_adjusted={ml_adjusted}"
        ),
    )

    log.info(
        "risk_calibration_complete",
        version=version,
        yellow=yellow,
        orange=orange,
        red=red,
        ml_yellow=ml_yellow,
        ml_orange=ml_orange,
        ml_red=ml_red,
        sample_size=report.total_flagged,
        ml_sample_size=report.ml_total_flagged,
        overall_precision=round(report.overall_precision, 4),
        adjusted=adjusted,
        ml_adjusted=ml_adjusted,
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
        ml_yellow_threshold=ml_yellow,
        ml_orange_threshold=ml_orange,
        ml_red_threshold=ml_red,
        ml_band_precisions=ml_band_precisions,
        ml_adjusted=ml_adjusted,
    )
