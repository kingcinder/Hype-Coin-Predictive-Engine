"""Tests for the local LLM prediction engine and ensemble integration."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from common.config import Settings
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
        # Force the health to be disconnected and recent so check_health is skipped
        engine._health = LLMHealth(connected=False, available=False, last_check=999999.0)
        # Close any existing client and replace with one that always fails
        engine.close()

        from common.http import build_httpx_client

        engine._client = build_httpx_client(base_url="http://127.0.0.1:1", timeout=0.1)
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
        engine.close()

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
        # H15: malformed LLM output is never stored verbatim as analysis.
        assert result.narrative_summary == ""
        assert result.risk_assessment == ""
        assert result.confidence_delta == 0.0

    def test_fallback_parse_never_stores_babble_verbatim(self) -> None:
        # Long, non-garbage-looking prose that is not JSON must still not be
        # persisted as narrative_summary (old behavior truncated it to 500
        # chars and stored it as if it were analysis).
        engine = LLMPredictionEngine()
        babble = (
            "The token shows remarkable momentum across several market venues "
            "with strong community engagement and increasing holder counts."
        )
        assert len(babble) >= 20
        result = engine._parse_response(7, "BABBLE", babble)
        assert result.narrative_summary == ""
        assert result.risk_assessment == ""
        assert result.confidence_delta == 0.0
        assert result.hype_delta == 0.0
        assert result.risk_delta == 0.0

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
        # Close any existing client and replace with one that always fails
        engine.close()
        from common.http import build_httpx_client

        engine._client = build_httpx_client(base_url="http://127.0.0.1:1", timeout=0.1)
        narrative, risk = engine.narrative_and_risk("TEST", {"momentum": 50.0})
        assert narrative == ""
        assert risk == ""
        engine.close()


def _configured_engine(provider: str) -> LLMPredictionEngine:
    engine = LLMPredictionEngine()
    engine.close()
    engine._provider = provider
    engine._base_url = "http://llm.test"
    engine._model = "test-model"
    engine._api_key = "test-key"
    engine._max_retries = 0
    engine._health = LLMHealth(connected=True, available=True, model="test-model")
    return engine


@pytest.mark.parametrize("provider", ["ollama", "openai_compat"])
@respx.mock
def test_provider_success_uses_expected_protocol(provider: str) -> None:
    raw = json.dumps(
        {
            "narrative": "Measured narrative from fixture.",
            "risk_assessment": "Measured risk summary from fixture.",
            "confidence_delta": 2,
            "hype_delta": 1,
            "risk_delta": -1,
            "key_factors": ["volume"],
        }
    )
    engine = _configured_engine(provider)
    route = "/api/generate" if provider == "ollama" else "/v1/chat/completions"
    response = (
        {"response": raw} if provider == "ollama" else {"choices": [{"message": {"content": raw}}]}
    )
    respx.post(f"http://llm.test{route}").mock(return_value=httpx.Response(200, json=response))

    result = engine.predict(1, "TEST", {}, 50, 25, 50)

    assert result.hype_delta == 1
    assert result.risk_delta == -1
    assert result.narrative_summary.startswith("Measured narrative")
    assert respx.calls.last.request.headers["Authorization"] == "Bearer test-key"
    engine.close()


@pytest.mark.parametrize("provider", ["ollama", "openai_compat"])
@pytest.mark.parametrize(
    ("status", "body"),
    [(200, {"response": "not json"}), (503, {"error": "busy"})],
)
@respx.mock
def test_provider_malformed_or_5xx_returns_no_signal(
    provider: str, status: int, body: dict[str, object]
) -> None:
    engine = _configured_engine(provider)
    route = "/api/generate" if provider == "ollama" else "/v1/chat/completions"
    response = body if provider == "ollama" else {"choices": [{"message": {"content": "not json"}}]}
    respx.post(f"http://llm.test{route}").mock(
        return_value=httpx.Response(status, json=response if status == 200 else body)
    )

    result = engine.predict(1, "TEST", {}, 50, 25, 50)

    assert result.narrative_summary == ""
    assert result.risk_assessment == ""
    assert (result.confidence_delta, result.hype_delta, result.risk_delta) == (0, 0, 0)
    engine.close()


@pytest.mark.parametrize("provider", ["ollama", "openai_compat"])
@respx.mock
@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadTimeout("slow")])
def test_provider_timeout_and_server_down_return_no_signal(provider: str, error: Exception) -> None:
    engine = _configured_engine(provider)
    route = "/api/generate" if provider == "ollama" else "/v1/chat/completions"
    respx.post(f"http://llm.test{route}").mock(side_effect=error)

    result = engine.predict(1, "TEST", {}, 50, 25, 50)

    assert result.narrative_summary == ""
    assert (result.confidence_delta, result.hype_delta, result.risk_delta) == (0, 0, 0)
    engine.close()


@pytest.mark.parametrize("provider", ["ollama", "openai_compat"])
@respx.mock
def test_health_checks_provider_endpoint(provider: str) -> None:
    engine = _configured_engine(provider)
    engine._health = LLMHealth()
    if provider == "ollama":
        respx.get("http://llm.test/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "test-model"}]})
        )
    else:
        respx.get("http://llm.test/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "test-model"}]})
        )

    health = engine.check_health()

    assert health.connected and health.available
    engine.close()


@respx.mock
def test_openai_compat_health_falls_back_to_health_route() -> None:
    engine = _configured_engine("openai_compat")
    engine._health = LLMHealth()
    respx.get("http://llm.test/v1/models").mock(return_value=httpx.Response(404))
    respx.get("http://llm.test/health").mock(return_value=httpx.Response(200, json={"ok": True}))

    health = engine.check_health()

    assert health.connected and health.available
    assert respx.calls.call_count == 2
    engine.close()


def test_structured_parser_accepts_fences_and_trailing_text() -> None:
    engine = LLMPredictionEngine()
    result = engine._parse_response(
        1,
        "TEST",
        'Here is the result:\n```json\n{"narrative":"valid", "hype_delta":2}\n```\nDone.',
    )

    assert result.narrative_summary == "valid"
    assert result.hype_delta == 2


def test_invalid_structured_fields_produce_no_signal() -> None:
    engine = LLMPredictionEngine()
    result = engine._parse_response(1, "TEST", '{"narrative": [], "hype_delta": "bad"}')

    assert result.narrative_summary == ""
    assert (result.confidence_delta, result.hype_delta, result.risk_delta) == (0, 0, 0)


def test_llm_settings_support_provider_and_legacy_ollama_url() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compat",
        llm_base_url="http://llm.test",
        llm_api_key="secret",
        llm_timeout_seconds=123,
    )
    assert settings.llm_provider == "openai_compat"
    assert settings.llm_base_url == "http://llm.test"
    assert settings.llm_api_key == "secret"
    assert settings.llm_timeout_seconds == 123

    legacy = Settings(_env_file=None, llm_ollama_url="http://ollama.test:11434")
    assert legacy.llm_ollama_url == "http://ollama.test:11434"
    assert Settings(_env_file=None).llm_model == "qwen2.5:7b"


def test_llm_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "91")

    settings = Settings(_env_file=None)

    assert settings.llm_provider == "openai_compat"
    assert settings.llm_base_url == "http://llm.test"
    assert settings.llm_api_key == "test-key"
    assert settings.llm_timeout_seconds == 91


@respx.mock
def test_5xx_retries_are_bounded_and_recover() -> None:
    engine = _configured_engine("openai_compat")
    engine._max_retries = 1
    route = respx.post("http://llm.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(503, json={"error": "busy"}),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"narrative":"ok"}'}}]},
        ),
    ]

    result = engine.predict(1, "TEST", {}, 50, 25, 50)

    assert route.call_count == 2
    assert result.narrative_summary == "ok"
    engine.close()


@respx.mock
def test_circuit_breaker_short_circuits_after_configured_failures() -> None:
    engine = _configured_engine("openai_compat")
    engine._breaker_failures = 2
    engine._max_retries = 0
    route = respx.post("http://llm.test/v1/chat/completions").mock(return_value=httpx.Response(503))

    for _ in range(3):
        result = engine.predict(1, "TEST", {}, 50, 25, 50)
        assert result.hype_delta == 0

    assert route.call_count == 2
    assert engine._circuit_open_until > 0
    engine.close()
