from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from common.config import get_settings
from common.enums import RiskBand
from common.time import utc_now
from storage import models
from storage.repository import record_health

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class RiskAssessment:
    band: RiskBand
    score: float
    reasons: list[str] = field(default_factory=list)
    hard_reject: bool = False


def _feature(features: dict[str, float], name: str, default: float = 0.0) -> float:
    value = features.get(name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def mask_unreliable_forecast(session, features: dict[str, float]) -> tuple[dict[str, float], bool]:
    """Remove the collapse forecast while calibration bias is red."""
    health = session.scalar(
        select(models.SystemHealth)
        .where(
            models.SystemHealth.component == "forecast_calibration",
        )
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    red = health is not None and health.state == "red"
    if not red:
        return features, False
    masked = dict(features)
    masked.pop("collapse_probability_24h", None)
    record_health(
        session,
        component="risk_forecast_fallback",
        state="yellow",
        message="collapse_probability_24h masked while forecast_calibration is red",
        ts=utc_now(),
    )
    return masked, True


def _apply_reason_weights(
    base_points: float,
    reason: str,
    weights: dict[str, float],
) -> float:
    """Apply adaptive weight to a risk reason's base point contribution.

    Reasons that historically predict collapses get amplified; reasons
    that rarely predict collapses get dampened.  The weight is applied
    as a multiplier centered on 1.0 (no change).
    """
    if not weights:
        return base_points
    # Match reason against weight keys (normalized prefix match)
    key = reason.split(":")[0].strip()[:60]
    weight = weights.get(key, 1.0)
    return base_points * weight


def assess_risk(
    features: dict[str, float],
    *,
    session: Session | None = None,
) -> RiskAssessment:
    """Assess risk using adaptive thresholds when calibration data exists.

    When a session is provided and calibration data is available, the band
    thresholds are learned from historical outcomes instead of using the
    hardcoded defaults.  The reason weights are also applied adaptively.
    """
    settings = get_settings()
    reasons: list[str] = []
    risk_points = 0.0
    hard_reject = False

    # Load adaptive thresholds and reason weights if session is available
    yellow_threshold = 25.0
    orange_threshold = 50.0
    red_threshold = 75.0
    reason_weights: dict[str, float] = {}
    if session is not None:
        try:
            from risk_engine.calibrator import get_current_thresholds, get_reason_weights

            yellow_threshold, orange_threshold, red_threshold = get_current_thresholds(session)
            reason_weights = get_reason_weights(session)
        except Exception:  # noqa: BLE001 - calibration lookup must not break scoring.
            pass

    liquidity = _feature(features, "liquidity_depth")
    suspicious_flags = _feature(features, "suspicious_contract_flags")
    concentration = _feature(features, "top_holder_concentration")
    pair_age = _feature(features, "pair_age_minutes")
    spread = _feature(features, "spread_estimate")
    buy_sell_ratio = _feature(features, "buy_sell_ratio", 1.0)
    holder_count = _feature(features, "holder_count")
    volatility = _feature(features, "volatility")
    withdrawal_signal = _feature(features, "liquidity_withdrawal_signal")
    lp_removal_signal = _feature(features, "lp_removal_signal")
    recidivism = _feature(features, "recidivism_score")
    collapse_probability = _feature(features, "collapse_probability_24h")
    lifecycle_phase = _feature(features, "lifecycle_phase", 1.0)

    if lifecycle_phase >= 4:
        pts = _apply_reason_weights(25.0, "Lifecycle collapse phase", reason_weights)
        risk_points += pts
        reasons.append("Lifecycle state machine reached collapse phase")
    elif lifecycle_phase >= 3:
        pts = _apply_reason_weights(10.0, "Lifecycle saturation phase", reason_weights)
        risk_points += pts
        reasons.append("Lifecycle state machine reached saturation phase")

    if liquidity <= 0 or liquidity < settings.black_min_liquidity_usd:
        hard_reject = True
        risk_points += 45
        reasons.append(f"Pair liquidity below hard minimum: ${liquidity:,.0f}")
    elif liquidity < settings.min_validated_liquidity_usd:
        pts = _apply_reason_weights(18.0, "Low liquidity", reason_weights)
        risk_points += pts
        reasons.append(f"Liquidity below validated speculative threshold: ${liquidity:,.0f}")

    if suspicious_flags >= 1:
        base = min(50, suspicious_flags * 20)
        pts = _apply_reason_weights(float(base), "Suspicious contract flags", reason_weights)
        risk_points += pts
        reasons.append(f"Suspicious contract/risk flags present: {suspicious_flags:.0f}")
    if suspicious_flags >= 3:
        hard_reject = True
        reasons.append("Multiple suspicious flags trigger hard reject")

    if withdrawal_signal >= 1:
        pts = _apply_reason_weights(25.0, "Liquidity withdrawal", reason_weights)
        risk_points += pts
        reasons.append(f"On-chain liquidity withdrawal detected: {withdrawal_signal:.0f} event(s)")
        if liquidity < settings.min_validated_liquidity_usd:
            hard_reject = True
            reasons.append("Liquidity withdrawal with a shallow book triggers hard reject")

    if lp_removal_signal >= 1:
        base = min(35.0, lp_removal_signal * 20.0)
        pts = _apply_reason_weights(base, "LP removal", reason_weights)
        risk_points += pts
        reasons.append(
            f"On-chain LP burn/withdrawal watcher detected {lp_removal_signal:.0f} "
            "fresh removal event(s)"
        )
        if liquidity < settings.min_validated_liquidity_usd:
            hard_reject = True
            reasons.append("Fresh LP removal with a shallow book triggers hard reject")

    if recidivism >= 60:
        pts = _apply_reason_weights(15.0, "Recidivism", reason_weights)
        risk_points += pts
        reasons.append(
            f"Launch wallets match known pump-and-dump clusters (recidivism {recidivism:.0f}/100)"
        )

    if collapse_probability >= 0.6:
        pts = _apply_reason_weights(20.0, "Collapse probability", reason_weights)
        risk_points += pts
        reasons.append(
            f"Forecast model assigns {collapse_probability:.0%} collapse probability within 24h"
        )

    if concentration >= 0.90:
        hard_reject = True
        risk_points += 35
        reasons.append(f"Extreme top-holder concentration: {concentration:.2%}")
    elif concentration >= 0.60:
        pts = _apply_reason_weights(20.0, "Holder concentration", reason_weights)
        risk_points += pts
        reasons.append(f"High top-holder concentration: {concentration:.2%}")

    if pair_age < 10 and liquidity < settings.min_validated_liquidity_usd:
        hard_reject = True
        risk_points += 25
        reasons.append("Pair too new with insufficient depth")

    if spread > 10:
        pts = _apply_reason_weights(12.0, "Spread", reason_weights)
        risk_points += pts
        reasons.append(f"Estimated spread/liquidity friction is high: {spread:.2f}")

    if buy_sell_ratio < 0.25:
        pts = _apply_reason_weights(10.0, "Sell pressure", reason_weights)
        risk_points += pts
        reasons.append(f"Sell pressure dominates buy flow: buy/sell={buy_sell_ratio:.2f}")

    if holder_count and holder_count < 50 and liquidity < settings.min_validated_liquidity_usd:
        pts = _apply_reason_weights(15.0, "Low holder count", reason_weights)
        risk_points += pts
        reasons.append(f"Low holder count for promoted watch status: {holder_count:.0f}")

    if volatility > 30:
        pts = _apply_reason_weights(8.0, "Volatility", reason_weights)
        risk_points += pts
        reasons.append(f"Extreme short-window volatility: {volatility:.2f}")

    score = max(0.0, min(100.0, risk_points))
    if hard_reject:
        return RiskAssessment(RiskBand.BLACK, max(score, 90.0), reasons, True)
    if score >= red_threshold:
        return RiskAssessment(RiskBand.RED, score, reasons)
    if score >= orange_threshold:
        return RiskAssessment(RiskBand.ORANGE, score, reasons)
    if score >= yellow_threshold:
        return RiskAssessment(RiskBand.YELLOW, score, reasons)
    return RiskAssessment(RiskBand.GREEN, score, reasons or ["No major structural danger detected"])
