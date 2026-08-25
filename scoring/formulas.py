from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.enums import RiskBand
from features.definitions import FEATURE_NAMES
from risk_engine.rules import assess_risk

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def score_return(value: float) -> float:
    return clamp(50.0 + value * 3.0)


def score_ratio(value: float) -> float:
    return clamp(50.0 * math.log2(max(value, 0.0) + 1.0))


def score_liquidity(value: float) -> float:
    if value <= 0:
        return 0.0
    return clamp((math.log10(value) - 3.5) * 35.0)


def inverse_score(value: float, *, max_bad: float) -> float:
    return clamp(100.0 * (1.0 - value / max_bad))


@dataclass(frozen=True)
class ScoreResult:
    hype: float
    ethos: float
    risk: float
    liquidity_access: float
    manipulation: float
    confidence: float
    uncertainty: float
    catalyst: float
    exit_risk: float
    research_priority: float
    risk_band: RiskBand
    drivers: dict[str, float]
    risk_reasons: list[str]
    missing_features: list[str]


def compute_scores(
    features: dict[str, float],
    missing_features: list[str] | None = None,
    *,
    data_layer_uncertainty: float = 0.0,
    session: Session | None = None,
) -> ScoreResult:
    missing_features = missing_features or []
    risk_assessment = assess_risk(features, session=session)

    r5 = score_return(features.get("five_min_return", 0.0))
    r1 = score_return(features.get("one_hour_return", 0.0))
    momentum = clamp((r5 * 0.35) + (r1 * 0.65))
    volume_accel = score_ratio(features.get("volume_acceleration", 0.0))
    buyer_breadth = clamp(features.get("unique_buyers_estimate", 0.0) * 2.0)
    social_velocity = clamp(features.get("mention_velocity", 0.0) * 10.0)
    liquidity_growth = score_return(features.get("liquidity_change", 0.0))
    liquidity_depth = score_liquidity(features.get("liquidity_depth", 0.0))
    venue_agreement = clamp(features.get("venue_agreement", 0.0))
    website = 100.0 if features.get("website_presence", 0.0) > 0 else 0.0
    github = 100.0 if features.get("github_presence_public", 0.0) > 0 else 0.0
    concentration = features.get("top_holder_concentration", 0.0)
    holder_quality = inverse_score(concentration * 100.0, max_bad=100.0)
    holder_growth = clamp(50.0 + features.get("holder_growth", 0.0) * 5.0)
    spread_quality = inverse_score(features.get("spread_estimate", 100.0), max_bad=25.0)
    volatility = features.get("volatility", 0.0)
    volatility_penalty = clamp(volatility * 1.5)
    flag_penalty = clamp(features.get("suspicious_contract_flags", 0.0) * 25.0)
    narrative = score_ratio(features.get("narrative_acceleration", 0.0))
    ignition = features.get("ignition_signal", 0.0)
    withdrawal_signal = features.get("liquidity_withdrawal_signal", 0.0)
    lp_removal_signal = features.get("lp_removal_signal", 0.0)
    recidivism = features.get("recidivism_score", 0.0)
    collapse_probability = features.get("collapse_probability_24h", 0.0)
    lifecycle_phase = features.get("lifecycle_phase", 1.0)
    catalyst_proximity = features.get("catalyst_proximity_hours", 168.0)
    cluster_growth = features.get("narrative_cluster_growth_7d", 0.0)
    channel_diversity = features.get("shill_channel_diversity", 0.0)
    prelaunch_priority = features.get("prelaunch_priority", 0.0)
    kol_velocity = features.get("kol_velocity", 0.0)
    star_velocity = features.get("github_star_velocity", 0.0)
    download_velocity = features.get("hf_download_velocity", 0.0)
    rpc_pool_health = clamp(features.get("rpc_pool_health", 1.0), 0.0, 1.0)

    hype = clamp(
        0.30 * momentum
        + 0.20 * volume_accel
        + 0.20 * buyer_breadth
        + 0.15 * social_velocity
        + 0.15 * liquidity_growth
        + 0.05 * clamp(ignition * 100.0)
        + 0.05 * clamp(cluster_growth * 5.0)
        + 0.05 * clamp(kol_velocity * 20.0)
        - 0.15 * flag_penalty
    )
    ethos = clamp(
        0.30 * holder_quality
        + 0.20 * (100.0 - flag_penalty)
        + 0.20 * liquidity_depth
        + 0.15 * ((website + github) / 2.0)
        + 0.15 * holder_growth
    )
    liquidity_access = clamp(
        0.55 * liquidity_depth + 0.25 * spread_quality + 0.20 * venue_agreement
    )
    manipulation = clamp(
        0.25 * max(0.0, social_velocity - buyer_breadth)
        + 0.20 * max(0.0, volume_accel - buyer_breadth)
        + 0.20 * flag_penalty
        + 0.15 * max(0.0, 100.0 - venue_agreement)
        + 0.20 * volatility_penalty
        + 0.05 * clamp(max(0.0, 100.0 - channel_diversity * 25.0))
    )

    available_ratio = 1.0 - (len(missing_features) / len(FEATURE_NAMES))
    data_layer_uncertainty = clamp(data_layer_uncertainty)
    confidence = clamp(100.0 * available_ratio - flag_penalty * 0.25 - data_layer_uncertainty)
    uncertainty = clamp(100.0 - confidence + len(missing_features) * 2.0 + data_layer_uncertainty)
    catalyst = clamp(
        0.55 * narrative
        + 0.25 * website
        + 0.20 * github
        + 0.15 * clamp(100.0 - catalyst_proximity * 5.0)
        + 0.05 * clamp(star_velocity)
        + 0.05 * clamp(download_velocity)
    )
    phase_penalty = clamp((lifecycle_phase - 2.0) * 25.0)  # parabolic=0, collapse=50
    exit_risk = clamp(
        0.40 * risk_assessment.score
        + 0.25 * volatility_penalty
        + 0.20 * max(0, -features.get("liquidity_change", 0.0))
        + 0.15 * (100.0 - spread_quality)
        + 0.20 * clamp(withdrawal_signal * 25.0)
        + 0.25 * clamp(lp_removal_signal * 25.0)
        + 0.15 * clamp(recidivism * 0.5)
        + 0.20 * clamp(collapse_probability * 100.0)
        + 0.10 * phase_penalty
    )

    research_priority = clamp(
        hype * (confidence / 100.0) * (liquidity_access / 100.0)
        - risk_assessment.score * 0.35
        - manipulation * 0.20
        - uncertainty * 0.10
        - 0.15 * clamp(collapse_probability * 100.0)
        + 0.05 * clamp(prelaunch_priority)
    )
    if risk_assessment.band == RiskBand.BLACK:
        research_priority = 0.0

    drivers = {
        "momentum": momentum,
        "volume_acceleration": volume_accel,
        "buyer_breadth": buyer_breadth,
        "social_velocity": social_velocity,
        "liquidity_growth": liquidity_growth,
        "liquidity_depth": liquidity_depth,
        "holder_quality": holder_quality,
        "venue_agreement": venue_agreement,
        "ignition_signal": clamp(ignition * 100.0),
        "liquidity_withdrawal_signal": clamp(withdrawal_signal * 100.0),
        "lp_removal_signal": clamp(lp_removal_signal * 100.0),
        "recidivism": recidivism,
        "collapse_probability_24h": clamp(collapse_probability * 100.0),
        "catalyst_proximity_hours": catalyst_proximity,
        "narrative_cluster_growth_7d": clamp(cluster_growth * 10.0),
        "shill_channel_diversity": channel_diversity,
        "prelaunch_priority": prelaunch_priority,
        "kol_velocity": kol_velocity,
        "github_star_velocity": clamp(star_velocity),
        "hf_download_velocity": clamp(download_velocity),
        "lifecycle_phase": lifecycle_phase,
        "rpc_pool_health": rpc_pool_health * 100.0,
        "rpc_pool_uncertainty": data_layer_uncertainty,
    }
    return ScoreResult(
        hype=round(hype, 4),
        ethos=round(ethos, 4),
        risk=round(risk_assessment.score, 4),
        liquidity_access=round(liquidity_access, 4),
        manipulation=round(manipulation, 4),
        confidence=round(confidence, 4),
        uncertainty=round(uncertainty, 4),
        catalyst=round(catalyst, 4),
        exit_risk=round(exit_risk, 4),
        research_priority=round(research_priority, 4),
        risk_band=risk_assessment.band,
        drivers=drivers,
        risk_reasons=risk_assessment.reasons,
        missing_features=missing_features,
    )
