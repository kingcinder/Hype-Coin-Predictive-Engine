"""LLM-enhanced ensemble scoring layer.

Extends the base ensemble (rule + ML + heuristic) with a 4th layer:
local LLM predictions from Ollama. The LLM layer gets a small default
weight (0.10) and adapts based on accuracy, just like the other layers.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.logging import get_logger
from scoring.ensemble import clamp

log = get_logger(__name__)

# Default LLM ensemble weight (added alongside rule, ml, heuristic)
_DEFAULT_LLM_WEIGHT = 0.10


@dataclass
class LLMEnsembleResult:
    """Result of applying LLM adjustments to the base ensemble score."""

    hype: float
    risk: float
    confidence: float
    research_priority: float
    narrative_summary: str
    risk_assessment: str
    key_factors: list[str]
    llm_weight: float
    applied: bool


def apply_llm_adjustments(
    base_hype: float,
    base_risk: float,
    base_confidence: float,
    *,
    llm_hype_delta: float = 0.0,
    llm_risk_delta: float = 0.0,
    llm_confidence_delta: float = 0.0,
    narrative_summary: str = "",
    risk_assessment: str = "",
    key_factors: list[str] | None = None,
    llm_weight: float = _DEFAULT_LLM_WEIGHT,
) -> LLMEnsembleResult:
    """Apply LLM prediction deltas to base ensemble scores.

    The LLM deltas are small adjustments (-5 to +5) that nudge the
    rule+ML+heuristic scores. The weight determines how much influence
    the LLM has on the final scores.
    """
    try:
        h_delta = max(-5.0, min(5.0, float(llm_hype_delta)))
        r_delta = max(-5.0, min(5.0, float(llm_risk_delta)))
        c_delta = max(-5.0, min(5.0, float(llm_confidence_delta)))
        w = max(0.0, min(0.3, float(llm_weight)))

        adjusted_hype = clamp(base_hype + h_delta * w * 2)
        adjusted_risk = clamp(base_risk + r_delta * w * 2)
        adjusted_confidence = clamp(base_confidence + c_delta * w * 2)

        # Research priority: recompute from adjusted hype/risk/confidence
        research_priority = clamp(
            adjusted_hype * (adjusted_confidence / 100.0) - adjusted_risk * 0.35
        )

        return LLMEnsembleResult(
            hype=round(adjusted_hype, 4),
            risk=round(adjusted_risk, 4),
            confidence=round(adjusted_confidence, 4),
            research_priority=round(research_priority, 4),
            narrative_summary=narrative_summary,
            risk_assessment=risk_assessment,
            key_factors=key_factors or [],
            llm_weight=w,
            applied=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("llm_ensemble_failed", error=str(exc))
        return LLMEnsembleResult(
            hype=base_hype,
            risk=base_risk,
            confidence=base_confidence,
            research_priority=0.0,
            narrative_summary="",
            risk_assessment="",
            key_factors=[],
            llm_weight=0.0,
            applied=False,
        )
