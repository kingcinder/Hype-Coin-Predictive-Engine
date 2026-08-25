"""Ensemble scoring — combines rule-based, ML forecast, and heuristic signals.

The ensemble produces a final ``EnsembleScore`` by blending:
- Rule-based scores from ``scoring.formulas.compute_scores`` (weight ~0.50)
- ML forecast probabilities from ``forecast.engine`` (weight ~0.30)
- Heuristic reliability from ``crawlers.heuristics`` (weight ~0.20)

Weights are adaptive: the calibrator adjusts them based on historical
accuracy of each scorer, shifting weight toward the most reliable signal
source over time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from common.logging import get_logger

log = get_logger(__name__)

# Default ensemble weights (rule, ml, heuristic)
_DEFAULT_WEIGHTS = {"rule": 0.50, "ml": 0.30, "heuristic": 0.20}


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
    correct_predictions: int = 0
    total_predictions: int = 0
    last_updated: float = field(default_factory=time.monotonic)

    @property
    def accuracy(self) -> float:
        if self.total_predictions == 0:
            return 0.5  # neutral prior
        return self.correct_predictions / self.total_predictions


class EnsembleEngine:
    """Adaptive ensemble that learns optimal weights from historical accuracy."""

    def __init__(self) -> None:
        self._accuracy: dict[str, ScorerAccuracy] = {
            name: ScorerAccuracy(scorer_name=name) for name in ("rule", "ml", "heuristic")
        }
        self._current_weights = dict(_DEFAULT_WEIGHTS)
        self._last_recalibrate = 0.0
        self._recalibrate_interval = 3600.0  # 1 hour

    def get_weights(self) -> dict[str, float]:
        """Return current ensemble weights, recalibrating if needed."""
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
        # Ensure minimum weight so no scorer is completely silenced
        for name in self._current_weights:
            self._current_weights[name] = max(0.10, self._current_weights[name])
        # Normalize back to sum=1
        total = sum(self._current_weights.values())
        if total > 0:
            for name in self._current_weights:
                self._current_weights[name] = round(self._current_weights[name] / total, 4)
        log.info("ensemble_recalibrated", weights=self._current_weights)

    def record_outcome(
        self,
        scorer_name: str,
        predicted_band: str,
        actual_outcome: str,
    ) -> None:
        """Record whether a scorer's prediction was correct.

        ``predicted_band`` is the risk band the scorer assigned.
        ``actual_outcome`` is 'positive' (token performed well) or
        'negative' (collapsed/rugged).
        """
        scorer = self._accuracy.get(scorer_name)
        if not scorer:
            return
        scorer.total_predictions += 1
        # GREEN predictions that survived = correct; RED/BLACK that collapsed = correct
        correct = (predicted_band in ("GREEN", "YELLOW") and actual_outcome == "positive") or (
            predicted_band in ("RED", "ORANGE", "BLACK") and actual_outcome == "negative"
        )
        if correct:
            scorer.correct_predictions += 1
        scorer.last_updated = time.monotonic()

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

        blended_hype = clamp(
            w_rule * rule_hype + w_ml * ml_h + w_heuristic * (rule_hype + h_score * 10) / 2
        )
        blended_risk = clamp(w_rule * rule_risk + w_ml * ml_r + w_heuristic * rule_risk)
        blended_confidence = clamp(
            w_rule * rule_confidence + w_ml * ml_c + w_heuristic * rule_confidence
        )

        # Research priority: recompute from blended hype/risk/confidence
        research_priority = clamp(blended_hype * (blended_confidence / 100.0) - blended_risk * 0.35)

        # Risk band from blended risk
        risk_band = _risk_band_from_score(blended_risk)

        # ML and heuristic contributions for explainability
        ml_contrib = abs(w_ml * (ml_h - rule_hype))
        heur_contrib = abs(w_heuristic * h_score * 10)

        return EnsembleScore(
            hype=round(blended_hype, 4),
            risk=round(blended_risk, 4),
            confidence=round(blended_confidence, 4),
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
