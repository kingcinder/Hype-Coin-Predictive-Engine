"""Ensemble scoring — combines rule-based, ML forecast, and heuristic signals.

The ensemble produces a final ``EnsembleScore`` by blending:
- Rule-based scores from ``scoring.formulas.compute_scores`` (weight ~0.50)
- ML forecast probabilities from ``forecast.engine`` (weight ~0.30)
- Heuristic reliability from ``crawlers.heuristics`` (weight ~0.20)

Weights are adaptive: the calibrator adjusts them based on historical
accuracy of each scorer, shifting weight toward the most reliable signal
source over time.

Confidence calibration maps the blended confidence to observed outcome
frequencies so scores reflect real-world reliability. Uncertainty from
the ML layer is propagated into the final confidence estimate."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from common.logging import get_logger
from common.time import utc_now

log = get_logger(__name__)

# Default ensemble weights (rule, ml, heuristic)
_DEFAULT_WEIGHTS = {"rule": 0.50, "ml": 0.30, "heuristic": 0.20}

# Dirty-flag periodic flush thresholds: record_outcome() is on the hot
# scoring path and must not write per outcome, but waiting for the hourly
# recalibration risks losing up to an hour of weight-adaptation progress on
# a crash.  Flush when EITHER 50 outcomes have been recorded since the last
# save OR 5 minutes have elapsed — whichever comes first.
_FLUSH_OUTCOME_THRESHOLD = 50
_FLUSH_INTERVAL_SECONDS = 300.0  # 5 minutes
# When a save keeps failing, don't retry on every outcome (that would
# reintroduce the N+1 write storm the dirty flag exists to avoid).  Retry at
# most once per this interval even while the counter is pinned above the
# threshold.
_FLUSH_RETRY_BACKOFF_SECONDS = 30.0


@dataclass
class EnsembleScore:
    """Blended score from all three scoring layers."""

    hype: float
    risk: float
    confidence: float
    research_priority: float
    risk_band: str
    rule_weight: float
    ml_weight: float
    heuristic_weight: float
    ml_contribution: float
    heuristic_contribution: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScorerAccuracy:
    """Tracks historical accuracy of a single scorer for weight adaptation."""

    scorer_name: str
    # Weighted by outcome confidence, so predictions accumulate as floats
    # (a 50%-confidence outcome counts 0.5, not 1).
    correct_predictions: float = 0.0
    total_predictions: float = 0.0
    last_updated: float = field(default_factory=time.monotonic)
    # Confidence calibration: maps reported confidence to observed accuracy
    # calibration_bucket_count tracks how many predictions fell in each
    # confidence bucket, and calibration_correct tracks how many were correct.
    calibration_buckets: dict[int, tuple[int, int]] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        if self.total_predictions == 0:
            return 0.5  # neutral prior
        return self.correct_predictions / self.total_predictions

    def record_calibration(self, confidence: float, correct: bool) -> None:
        """Record a calibration observation for confidence mapping."""
        bucket = int(confidence / 10) * 10  # 0-9, 10-19, ..., 90-100
        bucket = max(0, min(90, bucket))
        count, correct_count = self.calibration_buckets.get(bucket, (0, 0))
        self.calibration_buckets[bucket] = (
            count + 1,
            correct_count + (1 if correct else 0),
        )

    def calibrated_confidence(self, raw_confidence: float) -> float:
        """Map raw confidence to calibrated confidence using historical data.

        Falls back to the raw value when insufficient calibration data exists.
        """
        bucket = int(raw_confidence / 10) * 10
        bucket = max(0, min(90, bucket))
        count, correct_count = self.calibration_buckets.get(bucket, (0, 0))
        if count < 5:  # need at least 5 observations to calibrate
            return raw_confidence
        observed_accuracy = correct_count / count
        # Blend raw with observed: 70% observed, 30% raw for smoothness
        return round(observed_accuracy * 0.7 + (raw_confidence / 100.0) * 0.3 * 100, 2)


class EnsembleEngine:
    """Adaptive ensemble that learns optimal weights from historical accuracy.

    Weights are persisted to the database via ``EnsembleState`` so they
    survive process restarts and accumulate across scoring passes.
    """

    def __init__(self) -> None:
        self._accuracy: dict[str, ScorerAccuracy] = {
            name: ScorerAccuracy(scorer_name=name) for name in ("rule", "ml", "heuristic")
        }
        self._current_weights = dict(_DEFAULT_WEIGHTS)
        self._last_recalibrate = 0.0
        self._recalibrate_interval = 3600.0  # 1 hour
        self._persisted = False  # True once loaded from DB
        # Dirty-flag periodic flush state: outcomes recorded since the last
        # successful save, plus the monotonic timestamp of that save.
        self._pending_outcomes = 0
        self._last_flush = time.monotonic()
        # Timestamp of the last save *attempt* (success or failure) for the
        # retry backoff.
        self._last_flush_attempt = 0.0

    def _load_from_db(self) -> None:
        """Load persisted weights from the database if available."""
        if self._persisted:
            return
        try:
            from sqlalchemy import select

            from storage.database import session_scope
            from storage.models import EnsembleState

            with session_scope() as session:
                state = session.scalar(select(EnsembleState).limit(1))
                if state is not None:
                    self._current_weights = dict(state.current_weights)
                    for name, acc_data in (state.scorer_accuracy or {}).items():
                        if name in self._accuracy:
                            self._accuracy[name].correct_predictions = acc_data.get(
                                "correct_predictions", 0
                            )
                            self._accuracy[name].total_predictions = acc_data.get(
                                "total_predictions", 0
                            )
                    # Restore calibration buckets
                    for name, buckets in (state.calibration_buckets or {}).items():
                        if name in self._accuracy:
                            self._accuracy[name].calibration_buckets = {
                                int(k): tuple(v) for k, v in buckets.items()
                            }
                    log.info(
                        "ensemble_weights_loaded",
                        weights=self._current_weights,
                        total_predictions=state.total_predictions,
                    )
        except Exception:  # noqa: BLE001
            pass  # DB not available yet — use defaults
        self._persisted = True

    def _save_to_db(self) -> bool:
        """Persist current weights and scorer accuracy to the database.

        Returns ``True`` on success and ``False`` when the write failed
        (best-effort persistence).  On success the dirty-flag state is
        reset so the next flush starts from a clean slate; on failure the
        counters stay intact for a later retry (gated by the backoff).
        """
        try:
            from sqlalchemy import select

            from storage.database import session_scope
            from storage.models import EnsembleState

            scorer_data = {
                name: {
                    "correct_predictions": acc.correct_predictions,
                    "total_predictions": acc.total_predictions,
                    "accuracy": acc.accuracy,
                }
                for name, acc in self._accuracy.items()
            }
            cal_buckets = {
                name: {str(k): list(v) for k, v in acc.calibration_buckets.items()}
                for name, acc in self._accuracy.items()
            }
            total = int(round(sum(a.total_predictions for a in self._accuracy.values())))

            with session_scope() as session:
                state = session.scalar(select(EnsembleState).limit(1))
                if state is None:
                    # Single-row sentinel: the first writer owns id=1 so two
                    # threads racing on the read-then-create cannot both
                    # insert — the loser hits the PK conflict, is caught by
                    # the broad except, and simply retries via the backoff
                    # gate on the next flush (finding the row to update).
                    state = EnsembleState(
                        id=1,
                        current_weights=dict(self._current_weights),
                        scorer_accuracy=scorer_data,
                        calibration_buckets=cal_buckets,
                        total_predictions=total,
                        last_recalibrated_at=utc_now(),
                    )
                    session.add(state)
                else:
                    state.current_weights = dict(self._current_weights)
                    state.scorer_accuracy = scorer_data
                    state.calibration_buckets = cal_buckets
                    state.total_predictions = total
                    state.last_recalibrated_at = utc_now()
                # Append to weight history (keep last 200 entries)
                history = list(state.weight_history or [])
                history.append(
                    {
                        "ts": utc_now().isoformat(),
                        "weights": dict(self._current_weights),
                        "total_predictions": total,
                    }
                )
                state.weight_history = history[-200:]
                session.flush()
                # Commit is REQUIRED: flush() only emits SQL — without
                # commit the transaction is rolled back when the session
                # closes (``with session_scope()`` exit), discarding the save.
                # Follows the run_calibration flush-then-commit pattern.
                session.commit()
        except Exception:  # noqa: BLE001
            return False  # DB persistence is best-effort
        # Only reset the dirty-flag state on a successful save, so a failed
        # write leaves the counters intact for the next flush attempt.
        # ``_last_flush_attempt`` is cleared too: nothing is failing, so the
        # next threshold crossing should flush immediately instead of being
        # gated by a stale backoff.
        self._pending_outcomes = 0
        self._last_flush = time.monotonic()
        self._last_flush_attempt = 0.0
        return True

    def _maybe_flush(self) -> None:
        """Flush to the DB if the dirty-flag threshold was crossed.

        Saves when at least ``_FLUSH_OUTCOME_THRESHOLD`` outcomes have been
        recorded since the last successful save, or when more than
        ``_FLUSH_INTERVAL_SECONDS`` have elapsed since it — whichever comes
        first.  Keeps crash-window data loss bounded to ~50 outcomes / 5
        minutes without writing on every outcome.

        A failed save does NOT reset the dirty counter, so the next outcome
        would immediately trigger another attempt; the retry backoff gates
        those attempts to at most one per ``_FLUSH_RETRY_BACKOFF_SECONDS``
        to avoid hammering an unavailable DB.
        """
        elapsed = time.monotonic() - self._last_flush
        if self._pending_outcomes >= _FLUSH_OUTCOME_THRESHOLD or elapsed >= _FLUSH_INTERVAL_SECONDS:
            now = time.monotonic()
            if now - self._last_flush_attempt >= _FLUSH_RETRY_BACKOFF_SECONDS:
                self._last_flush_attempt = now
                self._save_to_db()

    def persist(self) -> None:
        """Explicitly persist current state to the database.

        Called by external code (e.g. engine shutdown) to ensure the
        latest weights are saved even if the hourly recalibration
        hasn't fired yet.
        """
        self._save_to_db()

    def get_weights(self) -> dict[str, float]:
        """Return current ensemble weights, recalibrating if needed."""
        self._load_from_db()
        now = time.monotonic()
        if (now - self._last_recalibrate) >= self._recalibrate_interval:
            self._recalibrate()
            self._last_recalibrate = now
        return dict(self._current_weights)

    def _recalibrate(self) -> None:
        """Adjust weights based on historical accuracy of each scorer."""
        total_acc = sum(s.accuracy for s in self._accuracy.values())
        if total_acc <= 0:
            return
        for name, scorer in self._accuracy.items():
            self._current_weights[name] = round(scorer.accuracy / total_acc, 4)
        # Dynamic minimum weight floor: shrinks as prediction count grows.
        # After 100+ predictions, floor drops to 0.05; after 500+, drops to 0.02.
        min_total = max(s.total_predictions for s in self._accuracy.values()) or 0
        if min_total >= 500:
            weight_floor = 0.02
        elif min_total >= 100:
            weight_floor = 0.05
        else:
            weight_floor = 0.10
        for name in self._current_weights:
            self._current_weights[name] = max(weight_floor, self._current_weights[name])
        # Normalize back to sum=1
        total = sum(self._current_weights.values())
        if total > 0:
            for name in self._current_weights:
                self._current_weights[name] = round(self._current_weights[name] / total, 4)
        log.info("ensemble_recalibrated", weights=self._current_weights)
        self._save_to_db()

    def record_outcome(
        self,
        scorer_name: str,
        predicted_band: str,
        actual_outcome: str,
        confidence: float | None = None,
        weight: float | None = None,
    ) -> None:
        """Record whether a scorer's prediction was correct.

        ``predicted_band`` is the risk band the scorer assigned.
        ``actual_outcome`` is 'positive' (token performed well) or
        'negative' (collapsed/rugged).
        ``confidence`` is the score confidence at prediction time (used for
        confidence calibration mapping AND, when ``weight`` is omitted, as
        the outcome weight so high-confidence predictions move the adaptive
        weights more than low-confidence guesses).
        ``weight`` explicitly overrides the confidence-derived weight
        (default 1.0 per outcome).
        """
        scorer = self._accuracy.get(scorer_name)
        if not scorer:
            return
        effective_weight = (
            weight
            if weight is not None
            else (max(0.1, min(1.0, float(confidence) / 100.0)) if confidence is not None else 1.0)
        )
        scorer.total_predictions += effective_weight
        # GREEN predictions that survived = correct; RED/BLACK that collapsed = correct
        correct = (predicted_band in ("GREEN", "YELLOW") and actual_outcome == "positive") or (
            predicted_band in ("RED", "ORANGE", "BLACK") and actual_outcome == "negative"
        )
        if correct:
            scorer.correct_predictions += effective_weight
        # Record calibration observation if confidence was provided
        if confidence is not None:
            scorer.record_calibration(confidence, correct)
        scorer.last_updated = time.monotonic()
        # Dirty-flag periodic flush: increment the counter, then save only
        # when 50 outcomes or 5 minutes have accumulated (whichever comes
        # first) — no per-outcome N+1 writes, but no hour-long loss window
        # either.
        self._pending_outcomes += 1
        self._maybe_flush()

    def record_outcomes(self, entries: list[dict[str, Any]]) -> None:
        """Record outcomes for a LIST of scorers in one call.

        Each entry is ``{"scorer_name": ..., "predicted_band": ...,
        "actual_outcome": ..., "confidence": ..., "weight": ...}`` — the
        per-token feedback path (``risk_engine.outcomes.evaluate_outcomes``)
        feeds rule + ml + heuristic in a single call so all three layers learn
        from the same token outcome without N separate invocations.
        """
        for entry in entries:
            self.record_outcome(
                scorer_name=entry["scorer_name"],
                predicted_band=entry["predicted_band"],
                actual_outcome=entry["actual_outcome"],
                confidence=entry.get("confidence"),
                weight=entry.get("weight"),
            )

    def blend(
        self,
        rule_hype: float,
        rule_risk: float,
        rule_confidence: float,
        rule_research_priority: float,
        rule_risk_band: str,
        ml_hype: float | None = None,
        ml_risk: float | None = None,
        ml_confidence: float | None = None,
        heuristic_score: float | None = None,
    ) -> EnsembleScore:
        """Blend rule-based, ML, and heuristic scores into a single result.

        Missing ML/heuristic scores are replaced with the rule-based value
        so the ensemble degrades gracefully when a layer is unavailable.
        """
        weights = self.get_weights()
        w_rule = weights["rule"]
        w_ml = weights["ml"]
        w_heuristic = weights["heuristic"]

        # Fill missing layers with rule-based values
        ml_h = ml_hype if ml_hype is not None else rule_hype
        ml_r = ml_risk if ml_risk is not None else rule_risk
        ml_c = ml_confidence if ml_confidence is not None else rule_confidence
        h_score = heuristic_score if heuristic_score is not None else 0.0

        # Heuristic contribution: boost hype for positive signals, boost risk
        # for negative signals (symmetric — heuristics affect both dimensions)
        heur_hype_boost = clamp(h_score * 10, 0, 30)  # capped at +30
        heur_risk_boost = clamp(-h_score * 5, 0, 20)  # negative h_score -> risk increase

        blended_hype = clamp(
            w_rule * rule_hype + w_ml * ml_h + w_heuristic * (rule_hype + heur_hype_boost) / 2
        )
        blended_risk = clamp(
            w_rule * rule_risk + w_ml * ml_r + w_heuristic * (rule_risk + heur_risk_boost) / 2
        )

        # ML uncertainty propagation: if ML confidence is low, reduce the
        # blended confidence proportionally (ML uncertainty bleeds through)
        ml_confidence_factor = ml_c / 100.0 if ml_confidence is not None else 1.0
        raw_blended_confidence = clamp(
            w_rule * rule_confidence + w_ml * ml_c + w_heuristic * rule_confidence
        )
        blended_confidence = clamp(raw_blended_confidence * ml_confidence_factor)

        # Research priority: recompute from blended hype/risk/confidence
        research_priority = clamp(blended_hype * (blended_confidence / 100.0) - blended_risk * 0.35)

        # Risk band from blended risk
        risk_band = _risk_band_from_score(blended_risk)

        # Confidence calibration: map raw blended confidence to observed accuracy
        # using the rule scorer's calibration data (most historical observations)
        rule_scorer = self._accuracy.get("rule")
        if rule_scorer is not None:
            calibrated_confidence = rule_scorer.calibrated_confidence(blended_confidence)
        else:
            calibrated_confidence = blended_confidence

        # ML and heuristic contributions for explainability
        ml_contrib = abs(w_ml * (ml_h - rule_hype))
        heur_contrib = abs(w_heuristic * heur_hype_boost)

        return EnsembleScore(
            hype=round(blended_hype, 4),
            risk=round(blended_risk, 4),
            confidence=round(calibrated_confidence, 4),
            research_priority=round(research_priority, 4),
            risk_band=risk_band,
            rule_weight=w_rule,
            ml_weight=w_ml,
            heuristic_weight=w_heuristic,
            ml_contribution=round(ml_contrib, 4),
            heuristic_contribution=round(heur_contrib, 4),
            details={
                "weights": weights,
                "rule_hype": rule_hype,
                "ml_hype": ml_h,
                "heuristic_score": h_score,
            },
        )


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _risk_band_from_score(risk: float) -> str:
    if risk >= 80:
        return "BLACK"
    if risk >= 60:
        return "RED"
    if risk >= 40:
        return "ORANGE"
    if risk >= 20:
        return "YELLOW"
    return "GREEN"


# Module-level singleton
ensemble_engine = EnsembleEngine()
