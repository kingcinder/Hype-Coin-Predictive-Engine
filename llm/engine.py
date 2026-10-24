"""Local LLM engine for hype-coin prediction enhancement.

Supports Ollama and OpenAI-compatible local servers. Provider failures return
neutral predictions so the LLM cannot disrupt rule-based scoring.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from common.config import get_settings
from common.http import build_httpx_client
from common.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OLLAMA_DEFAULT_URL = "http://localhost:11434"
OPENAI_COMPAT_DEFAULT_URL = "http://localhost:8080"
DEFAULT_MODEL = "qwen2.5:7b"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LLMPrediction:
    """Structured LLM prediction for a single token."""

    asset_id: int
    symbol: str
    narrative_summary: str
    risk_assessment: str
    confidence_delta: float  # -10 to +10 adjustment to rule-based confidence
    hype_delta: float  # -10 to +10 adjustment to rule-based hype
    risk_delta: float  # -10 to +10 adjustment to rule-based risk
    key_factors: list[str] = field(default_factory=list)
    llm_model: str = ""
    latency_ms: float = 0.0
    raw_response: str = ""


@dataclass
class LLMHealth:
    """Health status of the LLM connection."""

    connected: bool = False
    model: str = ""
    available: bool = False
    last_check: float = 0.0
    error: str = ""


class LLMResponse(BaseModel):
    """Validated structured output accepted by the scoring layer."""

    model_config = ConfigDict(extra="ignore")

    narrative: str = ""
    risk_assessment: str = ""
    confidence_delta: float = Field(default=0.0, allow_inf_nan=False)
    hype_delta: float = Field(default=0.0, allow_inf_nan=False)
    risk_delta: float = Field(default=0.0, allow_inf_nan=False)
    key_factors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class LLMPredictionEngine:
    """Local LLM engine for hype-coin analysis.

    Connects to the configured local provider and predicts for tokens.
    All methods degrade gracefully — if the provider is down or the model
    fails to respond, the engine returns neutral/empty predictions
    without raising exceptions.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._provider = self.settings.llm_provider
        default_url = (
            OPENAI_COMPAT_DEFAULT_URL if self._provider == "openai_compat" else OLLAMA_DEFAULT_URL
        )
        self._base_url = (
            self.settings.llm_base_url
            or (self.settings.llm_ollama_url if self._provider == "ollama" else default_url)
        ).rstrip("/")
        self._model = self.settings.llm_model or DEFAULT_MODEL
        self._api_key = self.settings.llm_api_key
        self._timeout_seconds = self.settings.llm_timeout_seconds
        self._max_retries = self.settings.llm_max_retries
        self._breaker_failures = self.settings.llm_circuit_breaker_failures
        self._breaker_seconds = self.settings.llm_circuit_breaker_seconds
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._health = LLMHealth()
        self._client: httpx.Client | None = None
        self._last_prompt_cache: dict[str, str] = {}

    @property
    def health(self) -> LLMHealth:
        return self._health

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            # Centralized construction with an explicit, sanitized proxy policy
            # (H26): a hostile proxy environment must never crash this.
            self._client = build_httpx_client(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_seconds),
                headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else None,
            )
        return self._client

    def close(self) -> None:
        """Shut down the HTTP client cleanly."""
        if self._client and not self._client.is_closed:
            self._client.close()
        self._client = None

    def check_health(self) -> LLMHealth:
        """Probe the configured provider and check model availability when listed."""
        try:
            client = self._get_client()
            if self._provider == "ollama":
                resp = self._retry_request(client.get, "/api/tags")
                models = resp.json().get("models", [])
                available_names = {m.get("name", "") for m in models if isinstance(m, dict)}
            else:
                resp = self._retry_request(client.get, "/v1/models")
                if resp.status_code in {404, 405}:
                    resp = self._retry_request(client.get, "/health")
                if resp.is_error:
                    resp.raise_for_status()
                payload = resp.json() if resp.content else {}
                models = payload.get("data", []) if isinstance(payload, dict) else []
                available_names = {item.get("id", "") for item in models if isinstance(item, dict)}
                if not available_names and resp.status_code < 400:
                    available_names = {self._model}
            if resp.is_error:
                resp.raise_for_status()
            model_ready = any(
                self._model == name or name.startswith(self._model.split(":")[0])
                for name in available_names
            )
            self._health = LLMHealth(
                connected=True,
                model=self._model,
                available=model_ready,
                last_check=time.time(),
            )
            log.info(
                "llm_health_check",
                connected=True,
                model=self._model,
                available=model_ready,
                models_found=list(available_names)[:5],
            )
        except Exception as exc:  # noqa: BLE001
            self._health = LLMHealth(
                connected=False,
                model=self._model,
                available=False,
                last_check=time.time(),
                error=str(exc),
            )
            log.debug("llm_health_check_failed", error=str(exc))
        return self._health

    def predict(
        self,
        asset_id: int,
        symbol: str,
        features: dict[str, float],
        rule_hype: float,
        rule_risk: float,
        rule_confidence: float,
        *,
        context: str = "",
    ) -> LLMPrediction:
        """Generate an LLM-enhanced prediction for a single token.

        Returns a structured prediction with deltas for hype, risk, and
        confidence. On any failure, returns neutral deltas (all zeros)
        so the scoring pipeline is never disrupted.
        """
        t0 = time.monotonic()
        try:
            prompt = self._build_prompt(
                symbol=symbol,
                features=features,
                rule_hype=rule_hype,
                rule_risk=rule_risk,
                rule_confidence=rule_confidence,
                context=context,
            )
            raw = self._call_provider(prompt)
            result = self._parse_response(asset_id, symbol, raw)
            result.latency_ms = (time.monotonic() - t0) * 1000
            result.llm_model = self._model
            result.raw_response = raw
            return result
        except Exception as exc:  # noqa: BLE001
            log.debug("llm_predict_failed", asset_id=asset_id, error=str(exc))
            return LLMPrediction(
                asset_id=asset_id,
                symbol=symbol,
                narrative_summary="",
                risk_assessment="",
                confidence_delta=0.0,
                hype_delta=0.0,
                risk_delta=0.0,
                latency_ms=(time.monotonic() - t0) * 1000,
                llm_model=self._model,
            )

    def batch_predict(
        self,
        tokens: list[dict[str, Any]],
        *,
        max_tokens: int = 10,
    ) -> list[LLMPrediction]:
        """Predict for multiple tokens. Caps to max_tokens to bound latency."""
        results: list[LLMPrediction] = []
        for token in tokens[:max_tokens]:
            result = self.predict(
                asset_id=token["asset_id"],
                symbol=token["symbol"],
                features=token.get("features", {}),
                rule_hype=token.get("rule_hype", 50.0),
                rule_risk=token.get("rule_risk", 25.0),
                rule_confidence=token.get("rule_confidence", 50.0),
                context=token.get("context", ""),
            )
            results.append(result)
        return results

    def narrative_and_risk(self, symbol: str, features: dict[str, float]) -> tuple[str, str]:
        """Get both narrative and risk analysis in a single LLM call."""
        try:
            feature_lines = []
            important = [
                ("momentum", "Hype momentum"),
                ("volume_acceleration", "Volume acceleration"),
                ("mention_velocity", "Social mentions/min"),
                ("liquidity_depth", "Liquidity depth"),
                ("liquidity_change", "Liquidity change"),
                ("top_holder_concentration", "Holder concentration"),
                ("volatility", "Volatility"),
                ("narrative_acceleration", "Narrative acceleration"),
                ("ignition_signal", "Ignition signal"),
                ("collapse_probability_24h", "Collapse probability (24h)"),
                ("lp_removal_signal", "LP removal signal"),
                ("suspicious_contract_flags", "Suspicious flags"),
                ("recidivism_score", "Recidivism score"),
            ]
            for key, label in important:
                value = features.get(key)
                if value is not None and value != 0.0:
                    feature_lines.append(f"- {label}: {value:.4f}")
            features_text = (
                "\n".join(feature_lines) if feature_lines else "- No significant features"
            )
            prompt = (
                f"Analyze crypto token ${symbol}.\n\n"
                f"Metrics:\n{features_text}\n\n"
                f"Respond with ONLY JSON (no markdown):\n"
                f'{{"narrative": "2-3 sentences", '
                f'"risk_assessment": "2-3 sentences"}}'
            )
            raw = self._call_provider(prompt, max_tokens=300)
            data = _parse_structured_output(raw)
            if data is None:
                return "", ""
            narrative = data.narrative
            risk = data.risk_assessment
            return (narrative if len(narrative) >= 20 else "", risk if len(risk) >= 20 else "")
        except Exception:  # noqa: BLE001
            return "", ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        symbol: str,
        features: dict[str, float],
        rule_hype: float,
        rule_risk: float,
        rule_confidence: float,
        context: str = "",
    ) -> str:
        """Build a structured prompt for token analysis."""
        feature_lines = []
        important_features = [
            ("momentum", "Hype momentum"),
            ("volume_acceleration", "Volume acceleration"),
            ("mention_velocity", "Social mention velocity"),
            ("liquidity_depth", "Liquidity depth"),
            ("liquidity_change", "Liquidity change"),
            ("top_holder_concentration", "Holder concentration"),
            ("volatility", "Volatility"),
            ("narrative_acceleration", "Narrative acceleration"),
            ("ignition_signal", "Ignition signal"),
            ("collapse_probability_24h", "Collapse probability (24h)"),
            ("lifecycle_phase", "Lifecycle phase"),
            ("recidivism_score", "Recidivism score"),
            ("lp_removal_signal", "LP removal signal"),
            ("kol_velocity", "KOL velocity"),
            ("github_star_velocity", "GitHub star velocity"),
        ]
        for key, label in important_features:
            value = features.get(key)
            if value is not None and value != 0.0:
                feature_lines.append(f"- {label}: {value:.4f}")

        features_text = "\n".join(feature_lines) if feature_lines else "- No significant features"
        context_text = f"\nAdditional context: {context}" if context else ""

        return (
            f"You are an expert crypto analyst specializing in speculative token research.\n"
            f"Analyze token ${symbol} and provide a JSON response.\n\n"
            f"Token: ${symbol}\n"
            f"Rule-based scores: hype={rule_hype:.1f}, risk={rule_risk:.1f}, "
            f"confidence={rule_confidence:.1f}\n\n"
            f"Key features:\n{features_text}{context_text}\n\n"
            f"Respond with ONLY a JSON object (no markdown, no explanation):\n"
            f'{{"narrative": "2-3 sentence analysis", '
            f'"risk_assessment": "2-3 sentence risk summary", '
            f'"confidence_delta": <number from -5 to +5>, '
            f'"hype_delta": <number from -5 to +5>, '
            f'"risk_delta": <number from -5 to +5>, '
            f'"key_factors": ["factor1", "factor2", "factor3"]}}'
        )

    def _call_ollama(self, prompt: str, max_tokens: int = 512) -> str:
        """Compatibility wrapper retained for callers of the former method."""
        return self._call_provider(prompt, max_tokens)

    def _call_provider(self, prompt: str, max_tokens: int = 512) -> str:
        """Call the selected provider and return its assistant response."""
        if time.monotonic() < self._circuit_open_until:
            raise RuntimeError("LLM circuit breaker is open")
        health = self._health
        if not health.connected and (time.time() - health.last_check) < 30:
            self._record_failure()
            raise httpx.ConnectError("LLM provider is unavailable")
        if not health.connected:
            self.check_health()
            if not self._health.connected:
                self._record_failure()
                raise httpx.ConnectError("LLM provider is unavailable")

        client = self._get_client()
        try:
            if self._provider == "ollama":
                payload = {
                    "model": self._model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": 0.3,
                        "top_p": 0.9,
                    },
                }
                resp = self._retry_request(client.post, "/api/generate", json=payload)
                resp.raise_for_status()
                answer = resp.json().get("response", "")
            else:
                payload = {
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                    "top_p": 0.9,
                }
                resp = self._retry_request(client.post, "/v1/chat/completions", json=payload)
                resp.raise_for_status()
                message = resp.json()["choices"][0]["message"]["content"]
                if isinstance(message, list):
                    answer = "".join(
                        part.get("text", "") for part in message if isinstance(part, dict)
                    )
                else:
                    answer = message
            if not isinstance(answer, str):
                raise ValueError("LLM response content is not text")
            self._failure_count = 0
            self._circuit_open_until = 0.0
            return answer
        except Exception:
            self._record_failure()
            raise

    def _retry_request(self, method: Any, path: str, **kwargs: Any) -> httpx.Response:
        retryer = Retrying(
            retry=retry_if_exception(_is_retryable),
            wait=wait_exponential_jitter(initial=0.1, max=0.5),
            stop=stop_after_attempt(self._max_retries + 1),
            reraise=True,
        )

        def request() -> httpx.Response:
            response = method(path, **kwargs)
            if response.status_code >= 500:
                response.raise_for_status()
            return response

        return retryer(request)

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self._breaker_failures:
            self._circuit_open_until = time.monotonic() + self._breaker_seconds

    def _parse_response(self, asset_id: int, symbol: str, raw: str) -> LLMPrediction:
        """Parse the LLM response into a structured prediction."""
        data = _parse_structured_output(raw)
        if data is None:
            return self._fallback_parse(asset_id, symbol, raw)

        return LLMPrediction(
            asset_id=asset_id,
            symbol=symbol,
            narrative_summary=data.narrative,
            risk_assessment=data.risk_assessment,
            confidence_delta=_clamp_delta(data.confidence_delta),
            hype_delta=_clamp_delta(data.hype_delta),
            risk_delta=_clamp_delta(data.risk_delta),
            key_factors=data.key_factors,
        )

    def _fallback_parse(self, asset_id: int, symbol: str, text: str) -> LLMPrediction:
        """Fallback when JSON extraction fails (H15).

        The model failed to honor the JSON contract, so its output is *not*
        trustworthy analysis: never store it verbatim as ``narrative_summary``.
        The old behavior persisted any >=20-char non-garbage response into the
        narrative surface (and from there into ``ScoreExplanation``) with no
        provenance marker distinguishing babble from structured output. Return
        neutral deltas and empty strings instead; the raw text remains
        available on ``LLMPrediction.raw_response`` (set by ``predict()``) for
        debugging.
        """
        return LLMPrediction(
            asset_id=asset_id,
            symbol=symbol,
            narrative_summary="",
            risk_assessment="",
            confidence_delta=0.0,
            hype_delta=0.0,
            risk_delta=0.0,
        )


def _clamp_delta(value: float, low: float = -5.0, high: float = 5.0) -> float:
    """Clamp an LLM delta to the safe range."""
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _parse_structured_output(raw: str) -> LLMResponse | None:
    """Find and validate the first JSON object, allowing fences and trailing prose."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(raw[index:])
            if not isinstance(parsed, dict):
                continue
            return LLMResponse.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError):
            continue
    return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

llm_engine = LLMPredictionEngine()
