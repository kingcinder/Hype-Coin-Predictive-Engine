"""Tests for the local LLM prediction engine and ensemble integration."""

from __future__ import annotations

import json

from llm.engine import LLMHealth, LLMPredictionEngine, _clamp_delta
from scoring.llm_ensemble import apply_llm_adjustments

# ---------------------------------------------------------------------------
# _clamp_delta
# ---------------------------------------------------------------------------


class TestClampDelta:
    def test_within_range(self) -> None:
        assert _clamp_delta(3.0) == 3.0

    def test_below_min(self) -> None:
        assert _clamp_delta(-10.0) == -5.0

    def test_above_max(self) -> None:
        assert _clamp_delta(10.0) == 5.0

    def test_non_numeric(self) -> None:
        assert _clamp_delta("not_a_number") == 0.0  # type: ignore[arg-type]

    def test_zero(self) -> None:
        assert _clamp_delta(0.0) == 0.0


# ---------------------------------------------------------------------------
# apply_llm_adjustments
# ---------------------------------------------------------------------------


class TestApplyLLMAdjustments:
    def test_no_adjustment(self) -> None:
        result = apply_llm_adjustments(base_hype=50.0, base_risk=25.0, base_confidence=60.0)
        assert result.applied is True
        assert result.hype == 50.0
        assert result.risk == 25.0
        assert result.confidence == 60.0

    def test_positive_hype_delta(self) -> None:
        result = apply_llm_adjustments(
            base_hype=50.0,
            base_risk=25.0,
            base_confidence=60.0,
            llm_hype_delta=5.0,
            llm_weight=0.10,
        )
        assert result.hype > 50.0
        assert result.applied is True

    def test_negative_risk_delta(self) -> None:
        result = apply_llm_adjustments(
            base_hype=50.0,
            base_risk=25.0,
            base_confidence=60.0,
            llm_risk_delta=-3.0,
            llm_weight=0.10,
        )
        assert result.risk < 25.0
        assert result.applied is True

    def test_zero_weight_no_change(self) -> None:
        result = apply_llm_adjustments(
            base_hype=50.0,
            base_risk=25.0,
            base_confidence=60.0,
            llm_hype_delta=5.0,
            llm_weight=0.0,
        )
        assert result.hype == 50.0
        assert result.risk == 25.0

    def test_clamping_stays_in_bounds(self) -> None:
        result = apply_llm_adjustments(
            base_hype=95.0,
            base_risk=5.0,
            base_confidence=50.0,
            llm_hype_delta=5.0,
            llm_weight=0.30,
        )
        assert 0.0 <= result.hype <= 100.0
        assert 0.0 <= result.risk <= 100.0

    def test_key_factors_pass_through(self) -> None:
        result = apply_llm_adjustments(
            base_hype=50.0,
            base_risk=25.0,
            base_confidence=60.0,
            key_factors=["factor1", "factor2"],
        )
        assert result.key_factors == ["factor1", "factor2"]

    def test_narrative_pass_through(self) -> None:
        result = apply_llm_adjustments(
            base_hype=50.0,
            base_risk=25.0,
            base_confidence=60.0,
            narrative_summary="Looks promising",
            risk_assessment="Low risk",
        )
        assert result.narrative_summary == "Looks promising"
        assert result.risk_assessment == "Low risk"


# ---------------------------------------------------------------------------
# LLMPredictionEngine
# ---------------------------------------------------------------------------


class TestLLMPredictionEngine:
    def test_initial_health(self) -> None:
        engine = LLMPredictionEngine()
        assert engine.health.connected is False
        assert engine.health.available is False

    def test_predict_returns_neutral_on_no_connection(self) -> None:
        engine = LLMPredictionEngine()
        # Ensure health check fails (no Ollama running)
        engine._health = LLMHealth(connected=False, available=False, last_check=999999.0)
        result = engine.predict(
            asset_id=1,
            symbol="TEST",
            features={"momentum": 50.0},
            rule_hype=50.0,
            rule_risk=25.0,
            rule_confidence=60.0,
        )
        assert result.confidence_delta == 0.0
        assert result.hype_delta == 0.0
        assert result.risk_delta == 0.0
        assert result.asset_id == 1
        assert result.symbol == "TEST"

    def test_parse_json_response(self) -> None:
        engine = LLMPredictionEngine()
        raw = json.dumps(
            {
                "narrative": "Strong momentum",
                "risk_assessment": "Moderate risk",
                "confidence_delta": 2.5,
                "hype_delta": 1.0,
                "risk_delta": -0.5,
                "key_factors": ["volume", "holders"],
            }
        )
        result = engine._parse_response(42, "FOO", raw)
        assert result.narrative_summary == "Strong momentum"
        assert result.risk_assessment == "Moderate risk"
        assert result.confidence_delta == 2.5
        assert result.hype_delta == 1.0
        assert result.risk_delta == -0.5
        assert result.key_factors == ["volume", "holders"]

    def test_parse_json_in_code_block(self) -> None:
        engine = LLMPredictionEngine()
        payload = {
            "narrative": "test",
            "risk_assessment": "ok",
            "confidence_delta": 1,
            "hype_delta": 0,
            "risk_delta": 0,
            "key_factors": [],
        }
        raw = f"```json\n{json.dumps(payload)}\n```"
        result = engine._parse_response(1, "T", raw)
        assert result.narrative_summary == "test"
        assert result.confidence_delta == 1.0

    def test_parse_invalid_json_returns_fallback(self) -> None:
        engine = LLMPredictionEngine()
        result = engine._parse_response(1, "T", "This is not JSON at all")
        assert result.narrative_summary == "This is not JSON at all"
        assert result.confidence_delta == 0.0

    def test_batch_predict_capped(self) -> None:
        engine = LLMPredictionEngine()
        engine._health = LLMHealth(connected=False, available=False, last_check=999999.0)
        tokens = [{"asset_id": i, "symbol": f"T{i}", "features": {}} for i in range(20)]
        results = engine.batch_predict(tokens, max_tokens=5)
        assert len(results) == 5

    def test_close(self) -> None:
        engine = LLMPredictionEngine()
        engine._get_client()
        engine.close()
        assert engine._client is None

    def test_narrative_and_risk_returns_tuple(self) -> None:
        engine = LLMPredictionEngine()
        engine._health = LLMHealth(connected=False, available=False, last_check=999999.0)
        narrative, risk = engine.narrative_and_risk("TEST", {"momentum": 50.0})
        assert narrative == ""
        assert risk == ""
