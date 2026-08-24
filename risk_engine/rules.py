from __future__ import annotations

from dataclasses import dataclass, field

from common.config import get_settings
from common.enums import RiskBand


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


def assess_risk(features: dict[str, float]) -> RiskAssessment:
    settings = get_settings()
    reasons: list[str] = []
    risk_points = 0.0
    hard_reject = False

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
        risk_points += 25
        reasons.append("Lifecycle state machine reached collapse phase")
    elif lifecycle_phase >= 3:
        risk_points += 10
        reasons.append("Lifecycle state machine reached saturation phase")

    if liquidity <= 0 or liquidity < settings.black_min_liquidity_usd:
        hard_reject = True
        risk_points += 45
        reasons.append(f"Pair liquidity below hard minimum: ${liquidity:,.0f}")
    elif liquidity < settings.min_validated_liquidity_usd:
        risk_points += 18
        reasons.append(f"Liquidity below validated speculative threshold: ${liquidity:,.0f}")

    if suspicious_flags >= 1:
        risk_points += min(50, suspicious_flags * 20)
        reasons.append(f"Suspicious contract/risk flags present: {suspicious_flags:.0f}")
    if suspicious_flags >= 3:
        hard_reject = True
        reasons.append("Multiple suspicious flags trigger hard reject")

    if withdrawal_signal >= 1:
        risk_points += 25
        reasons.append(
            f"On-chain liquidity withdrawal detected: {withdrawal_signal:.0f} event(s)"
        )
        if liquidity < settings.min_validated_liquidity_usd:
            hard_reject = True
            reasons.append("Liquidity withdrawal with a shallow book triggers hard reject")

    if lp_removal_signal >= 1:
        risk_points += min(35.0, lp_removal_signal * 20.0)
        reasons.append(
            f"On-chain LP burn/withdrawal watcher detected {lp_removal_signal:.0f} "
            "fresh removal event(s)"
        )
        if liquidity < settings.min_validated_liquidity_usd:
            hard_reject = True
            reasons.append("Fresh LP removal with a shallow book triggers hard reject")

    if recidivism >= 60:
        risk_points += 15
        reasons.append(
            f"Launch wallets match known pump-and-dump clusters (recidivism {recidivism:.0f}/100)"
        )

    if collapse_probability >= 0.6:
        risk_points += 20
        reasons.append(
            f"Forecast model assigns {collapse_probability:.0%} collapse probability within 24h"
        )

    if concentration >= 0.90:
        hard_reject = True
        risk_points += 35
        reasons.append(f"Extreme top-holder concentration: {concentration:.2%}")
    elif concentration >= 0.60:
        risk_points += 20
        reasons.append(f"High top-holder concentration: {concentration:.2%}")

    if pair_age < 10 and liquidity < settings.min_validated_liquidity_usd:
        hard_reject = True
        risk_points += 25
        reasons.append("Pair too new with insufficient depth")

    if spread > 10:
        risk_points += 12
        reasons.append(f"Estimated spread/liquidity friction is high: {spread:.2f}")

    if buy_sell_ratio < 0.25:
        risk_points += 10
        reasons.append(f"Sell pressure dominates buy flow: buy/sell={buy_sell_ratio:.2f}")

    if holder_count and holder_count < 50 and liquidity < settings.min_validated_liquidity_usd:
        risk_points += 15
        reasons.append(f"Low holder count for promoted watch status: {holder_count:.0f}")

    if volatility > 30:
        risk_points += 8
        reasons.append(f"Extreme short-window volatility: {volatility:.2f}")

    score = max(0.0, min(100.0, risk_points))
    if hard_reject:
        return RiskAssessment(RiskBand.BLACK, max(score, 90.0), reasons, True)
    if score >= 75:
        return RiskAssessment(RiskBand.RED, score, reasons)
    if score >= 50:
        return RiskAssessment(RiskBand.ORANGE, score, reasons)
    if score >= 25:
        return RiskAssessment(RiskBand.YELLOW, score, reasons)
    return RiskAssessment(RiskBand.GREEN, score, reasons or ["No major structural danger detected"])
