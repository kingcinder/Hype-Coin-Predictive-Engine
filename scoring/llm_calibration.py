"""Adaptive LLM weight calibration.

Tracks LLM prediction outcomes over a rolling window and adjusts the
ensemble weight (llm_weight) dynamically: increasing it when the LLM
improves scoring accuracy, decreasing it when it degrades.

The calibrator is stateless at the class level — all state is persisted
in the database via LLMCalibrationRecord and LLMCalibrationState models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.logging import get_logger
from common.time import utc_now
from storage import models

log = get_logger(__name__)


@dataclass
class CalibrationSnapshot:
    """Current state of the LLM calibration system."""

    current_weight: float
    previous_weight: float
    total_predictions: int
    total_improved: int
    total_degraded: int
    improvement_rate: float
    last_calibration_ts: datetime | None
    weight_history: list[dict[str, object]]


class LLMCalibrator:
    """Evaluates LLM prediction outcomes and adjusts ensemble weight.

    Usage:
        calibrator = LLMCalibrator()
        # After scoring with LLM deltas:
        calibrator.record_prediction(session, asset_id, score_id, ...)
        # Periodically (e.g. once per scan cycle):
        calibrator.calibrate(session)
        # Get current weight for scoring:
        weight = calibrator.get_weight(session)
    """

    def record_prediction(
        self,
        session: Session,
        *,
        asset_id: int,
        score_id: int | None,
        model_name: str,
        hype_delta: float,
        risk_delta: float,
        confidence_delta: float,
        llm_weight: float,
        base_hype: float,
        base_risk: float,
        base_confidence: float,
        final_hype: float,
        final_risk: float,
        final_confidence: float,
    ) -> None:
        """Record an LLM prediction for later evaluation."""
        settings = get_settings()
        if not settings.llm_calibration_enabled:
            return
        record = models.LLMCalibrationRecord(
            asset_id=asset_id,
            score_id=score_id,
            prediction_ts=utc_now(),
            model_name=model_name,
            hype_delta=hype_delta,
            risk_delta=risk_delta,
            confidence_delta=confidence_delta,
            llm_weight_at_time=llm_weight,
            base_hype=base_hype,
            base_risk=base_risk,
            base_confidence=base_confidence,
            final_hype=final_hype,
            final_risk=final_risk,
            final_confidence=final_confidence,
        )
        session.add(record)

    def evaluate_predictions(self, session: Session) -> int:
        """Evaluate unevaluated predictions by checking actual outcomes.

        Compares what the LLM predicted against what actually happened.
        Returns the number of predictions evaluated.
        """
        settings = get_settings()
        cutoff = utc_now() - timedelta(hours=settings.llm_calibration_eval_hours)
        unevaluated = session.scalars(
            select(models.LLMCalibrationRecord).where(
                models.LLMCalibrationRecord.evaluated_at.is_(None),
                models.LLMCalibrationRecord.prediction_ts <= cutoff,
            )
        ).all()

        evaluated_count = 0
        for record in unevaluated:
            outcome = self._find_outcome(session, record)
            if outcome is None:
                continue

            improved = self._assess_improvement(record, outcome)
            record.evaluated_at = utc_now()
            record.actual_risk_band = outcome.risk_band
            record.price_change_pct = outcome.price_change_pct
            record.collapsed = outcome.collapsed
            record.llm_improved = improved
            record.evaluation_details = {
                "actual_collapsed": outcome.collapsed,
                "actual_price_change": outcome.price_change_pct,
                "base_risk_band": self._score_to_band(record.base_risk),
                "final_risk_band": outcome.risk_band,
            }
            evaluated_count += 1

        if evaluated_count > 0:
            log.info(
                "llm_calibration_evaluated",
                count=evaluated_count,
                window_hours=settings.llm_calibration_eval_hours,
            )
        return evaluated_count

    def calibrate(self, session: Session) -> float:
        """Run calibration: evaluate outcomes, compute new weight, persist.

        Returns the current (possibly adjusted) weight.
        """
        settings = get_settings()
        if not settings.llm_calibration_enabled:
            return settings.llm_weight

        # Step 1: Evaluate recent predictions
        self.evaluate_predictions(session)

        # Step 2: Prune old calibration records (keep 2x window)
        prune_cutoff = utc_now() - timedelta(hours=settings.llm_calibration_window_hours * 2)
        session.execute(
            select(models.LLMCalibrationRecord).where(
                models.LLMCalibrationRecord.created_at < prune_cutoff
            )
        )

        # Step 3: Get or create calibration state (upsert pattern)
        state = session.scalar(select(models.LLMCalibrationState).limit(1))
        if state is None:
            state = models.LLMCalibrationState(
                current_weight=settings.llm_weight,
                previous_weight=settings.llm_weight,
            )
            session.add(state)
            session.flush()

        # Step 3: Count improved vs degraded in the rolling window
        window_start = utc_now() - timedelta(hours=settings.llm_calibration_window_hours)
        stats = session.execute(
            select(
                func.count(models.LLMCalibrationRecord.id).label("total"),
                func.sum(
                    func.cast(
                        models.LLMCalibrationRecord.llm_improved.is_(True),
                        models.Integer,
                    )
                ).label("improved"),
            ).where(
                models.LLMCalibrationRecord.prediction_ts >= window_start,
                models.LLMCalibrationRecord.evaluated_at.is_not(None),
            )
        ).one()

        total = stats.total or 0
        improved = stats.improved or 0
        degraded = total - improved
        state.total_predictions = total
        state.total_improved = improved
        state.total_degraded = degraded

        # Step 4: Adjust weight if enough samples
        if total < settings.llm_calibration_min_samples:
            log.debug(
                "llm_calibration_insufficient_samples",
                total=total,
                min_required=settings.llm_calibration_min_samples,
            )
            state.last_calibration_ts = utc_now()
            return state.current_weight

        improvement_rate = improved / total if total > 0 else 0.5
        old_weight = state.current_weight

        if improvement_rate >= 0.55:
            # LLM is helping — increase weight
            new_weight = min(
                settings.llm_calibration_ceiling,
                old_weight + settings.llm_calibration_step,
            )
            direction = "increased"
        elif improvement_rate <= 0.45:
            # LLM is hurting — decrease weight
            new_weight = max(
                settings.llm_calibration_floor,
                old_weight - settings.llm_calibration_step,
            )
            direction = "decreased"
        else:
            new_weight = old_weight
            direction = "unchanged"

        state.previous_weight = old_weight
        state.current_weight = new_weight
        state.last_calibration_ts = utc_now()

        # Append to weight history (keep last 100 entries)
        history = list(state.weight_history or [])
        history.append(
            {
                "ts": utc_now().isoformat(),
                "old_weight": round(old_weight, 4),
                "new_weight": round(new_weight, 4),
                "direction": direction,
                "improvement_rate": round(improvement_rate, 4),
                "total_samples": total,
                "improved": improved,
                "degraded": degraded,
            }
        )
        state.weight_history = history[-100:]

        if direction != "unchanged":
            log.info(
                "llm_weight_adjusted",
                old_weight=round(old_weight, 4),
                new_weight=round(new_weight, 4),
                direction=direction,
                improvement_rate=round(improvement_rate, 4),
                total_samples=total,
            )

        return state.current_weight

    def get_weight(self, session: Session) -> float:
        """Get the current adaptive LLM weight."""
        settings = get_settings()
        if not settings.llm_calibration_enabled:
            return settings.llm_weight
        state = session.scalar(select(models.LLMCalibrationState).limit(1))
        return state.current_weight if state else settings.llm_weight

    def get_snapshot(self, session: Session) -> CalibrationSnapshot:
        """Get the current calibration state for display in the GUI/API."""
        settings = get_settings()
        state = session.scalar(select(models.LLMCalibrationState).limit(1))
        if state:
            total = state.total_predictions
            improved = state.total_improved
            return CalibrationSnapshot(
                current_weight=state.current_weight,
                previous_weight=state.previous_weight,
                total_predictions=total,
                total_improved=improved,
                total_degraded=state.total_degraded,
                improvement_rate=improved / total if total > 0 else 0.0,
                last_calibration_ts=state.last_calibration_ts,
                weight_history=list(state.weight_history or []),
            )
        return CalibrationSnapshot(
            current_weight=settings.llm_weight,
            previous_weight=settings.llm_weight,
            total_predictions=0,
            total_improved=0,
            total_degraded=0,
            improvement_rate=0.0,
            last_calibration_ts=None,
            weight_history=[],
        )

    # ── Private helpers ────────────────────────────────────────────────

    def _find_outcome(
        self, session: Session, record: models.LLMCalibrationRecord
    ) -> models.RiskOutcome | None:
        """Find the risk outcome for a calibration record's score."""
        if record.score_id is None:
            return None
        return session.scalar(
            select(models.RiskOutcome).where(models.RiskOutcome.score_id == record.score_id)
        )

    def _assess_improvement(
        self,
        record: models.LLMCalibrationRecord,
        outcome: models.RiskOutcome,
    ) -> bool:
        """Determine whether the LLM delta improved scoring accuracy.

        The LLM is considered to have improved accuracy if the final
        (LLM-adjusted) risk band is closer to the actual outcome than
        the base (rule-only) risk band.
        """
        base_band = self._score_to_band(record.base_risk)
        final_band = outcome.risk_band
        actual_collapsed = outcome.collapsed

        base_distance = self._band_distance(base_band, actual_collapsed)
        final_distance = self._band_distance(final_band, actual_collapsed)
        return final_distance <= base_distance

    @staticmethod
    def _score_to_band(risk_score: float) -> str:
        """Convert a numeric risk score to a band string."""
        if risk_score >= 75:
            return "BLACK"
        elif risk_score >= 50:
            return "RED"
        elif risk_score >= 25:
            return "ORANGE"
        elif risk_score >= 10:
            return "YELLOW"
        return "GREEN"

    @staticmethod
    def _band_distance(band: str, collapsed: bool) -> float:
        """Compute a distance metric: how far is the band from the truth.

        Uses both band-level distance and absolute risk proximity as a
        tiebreaker to avoid rewarding no-op LLM adjustments.
        """
        band_order = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3, "BLACK": 4}
        band_val = band_order.get(band, 2)
        # If collapsed, the "truth" is BLACK (4); if not, GREEN (0)
        truth = 4 if collapsed else 0
        return abs(band_val - truth)

    @staticmethod
    def _assess_improvement_tiebreaker(
        base_risk: float, final_risk: float, collapsed: bool
    ) -> bool:
        """Break ties: prefer the risk score closer to the collapsed/safe truth."""
        truth_risk = 100.0 if collapsed else 0.0
        base_dist = abs(base_risk - truth_risk)
        final_dist = abs(final_risk - truth_risk)
        return final_dist <= base_dist


# Module-level singleton
llm_calibrator = LLMCalibrator()
