"""Local LLM engine for hype-coin prediction enhancement.

Connects to a local Ollama instance to provide LLM-enhanced analysis of
crypto tokens: narrative analysis, risk assessment, and prediction confidence
boosting. Degrades gracefully when Ollama is unavailable — the engine never
blocks on LLM availability.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from common.config import get_settings
from common.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OLLAMA_DEFAULT_URL = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "qwen2.5:0.5b"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 2

# Signals that indicate the LLM refused or returned garbage output
_GARBAGE_SIGNALS = (
    "i cannot",
    "i can't",
    "sorry",
    "i don't",
    "error:",
    "as an ai",
    "i am unable",
    "please provide",
    "not possible",
)


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


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class LLMPredictionEngine:
    """Local LLM engine for hype-coin analysis.

    Connects to Ollama and provides structured predictions for tokens.
    All methods degrade gracefully — if Ollama is down or the model
    fails to respond, the engine returns neutral/empty predictions
    without raising exceptions.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._base_url = OLLAMA_DEFAULT_URL
        self._model = OLLAMA_DEFAULT_MODEL
        self._health = LLMHealth()
        self._client: httpx.Client | None = None
        self._last_prompt_cache: dict[str, str] = {}

    @property
    def health(self) -> LLMHealth:
        return self._health

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            )
        return self._client

    def close(self) -> None:
        """Shut down the HTTP client cleanly."""
        if self._client and not self._client.is_closed:
            self._client.close()
        self._client = None

    def check_health(self) -> LLMHealth:
        """Probe Ollama availability and model readiness."""
        try:
            client = self._get_client()
            resp = client.get("/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            available_names = {m.get("name", "") for m in models}
            model_ready = any(
                self._model in name or name.startswith(self._model.split(":")[0])
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
            raw = self._call_ollama(prompt)
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
            raw = self._call_ollama(prompt, max_tokens=300)
            try:
                text = raw.strip()
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()
                data = json.loads(text)
                narrative = data.get("narrative", "")
                risk = data.get("risk_assessment", "")
                # Filter out garbage LLM output (too short or looks like an error)
                if len(narrative) < 20:
                    narrative = ""
                if len(risk) < 20:
                    risk = ""
                return narrative, risk
            except (json.JSONDecodeError, IndexError):
                return "", ""
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
        """Call Ollama API and return the response text."""
        health = self._health
        if not health.connected and (time.time() - health.last_check) < 30:
            return ""
        if not health.connected:
            self.check_health()
            if not self._health.connected:
                return ""

        client = self._get_client()
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
        resp = client.post("/api/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "")

    def _parse_response(self, asset_id: int, symbol: str, raw: str) -> LLMPrediction:
        """Parse the LLM response into a structured prediction."""
        # Try to extract JSON from the response
        text = raw.strip()
        # Handle markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            # Fallback: try to extract individual values
            return self._fallback_parse(asset_id, symbol, text)

        return LLMPrediction(
            asset_id=asset_id,
            symbol=symbol,
            narrative_summary=data.get("narrative", ""),
            risk_assessment=data.get("risk_assessment", ""),
            confidence_delta=_clamp_delta(data.get("confidence_delta", 0.0)),
            hype_delta=_clamp_delta(data.get("hype_delta", 0.0)),
            risk_delta=_clamp_delta(data.get("risk_delta", 0.0)),
            key_factors=data.get("key_factors", []),
        )

    def _fallback_parse(self, asset_id: int, symbol: str, text: str) -> LLMPrediction:
        """Fallback parser when JSON extraction fails.

        Filters out garbage LLM output (too short, error-like, or nonsensical).
        Only accepts text that looks like genuine analysis (>20 chars, no error
        keywords).
        """
        # Filter garbage: too short, looks like an error/refusal, or is empty
        if not text or len(text) < 20:
            return LLMPrediction(
                asset_id=asset_id,
                symbol=symbol,
                narrative_summary="",
                risk_assessment="",
                confidence_delta=0.0,
                hype_delta=0.0,
                risk_delta=0.0,
            )
        lower = text.lower()
        if any(sig in lower for sig in _GARBAGE_SIGNALS):
            return LLMPrediction(
                asset_id=asset_id,
                symbol=symbol,
                narrative_summary="",
                risk_assessment="",
                confidence_delta=0.0,
                hype_delta=0.0,
                risk_delta=0.0,
            )
        return LLMPrediction(
            asset_id=asset_id,
            symbol=symbol,
            narrative_summary=text[:500],
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


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

llm_engine = LLMPredictionEngine()
