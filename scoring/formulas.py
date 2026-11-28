from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.enums import RiskBand
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


# Band → survival confidence now comes from scoring/band_calibration.py, which
# derives it from persisted, evaluated RiskOutcome rows and only publishes it as
# *validated* when every band has enough samples and survival is monotone. The
# hardcoded table it replaced was an assertion nothing checked; the fallback
# below is used (and reported unvalidated) until the outcomes earn it.


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
    confidence_source: str = "fallback"


def compute_scores(
    features: dict[str, float],
    missing_features: list[str] | None = None,
    *,
    data_layer_uncertainty: float = 0.0,
    session: Session | None = None,
) -> ScoreResult:
    missing_features = missing_features or []
    # PITFIX 2026-11-13: assess_risk tracks missing inputs via _feature()
    # defaults, but the live path passed every key (missing ones as 0.0), so
    # missing was invisible to it and missing liquidity read as 0 liquidity
    # -> hard BLACK. Strip missing keys here so missing is missing, live
    # and in rescore alike. compute_scores still uses the full dict below
    # for its own components, which handle 0.0 defaults explicitly.
    _missing = set(missing_features)
    _assess_features = {k: v for k, v in features.items() if k not in _missing}
    risk_assessment = assess_risk(_assess_features, session=session)
    # E1: the honest coverage set is the union of factory-flagged missing
    # features and the inputs that silently hit their rule-engine default
    # (e.g. collapse_probability_24h after mask_unreliable_forecast pops it).
    all_missing = sorted(set(missing_features) | set(risk_assessment.missing_features))

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

    # The positive components are a weighted average (weights sum to 1.15, so
    # the sum is divided by 1.15); the flag penalty is applied separately and
    # is NOT part of the average. This keeps hype on the 0-100 scale instead
    # of inflating it ~15% before the clamp.
    hype = clamp(
        (
            0.30 * momentum
            + 0.20 * volume_accel
            + 0.20 * buyer_breadth
            + 0.15 * social_velocity
            + 0.15 * liquidity_growth
            + 0.05 * clamp(ignition * 100.0)
            + 0.05 * clamp(cluster_growth * 5.0)
            + 0.05 * clamp(kol_velocity * 20.0)
        )
        / 1.15
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

    # NOTE (v4b, 2026-11-20): the E1 coverage penalty is retired — it no
    # longer feeds confidence (v4: confidence is the calibrated survival
    # probability) or uncertainty (v4b: uncertainty is the predictive
    # entropy). Missing-feature evidence is still visible via
    # ScoreResult.missing_features and the drivers, not via a penalty.
    data_layer_uncertainty = clamp(data_layer_uncertainty)
    # Confidence derives from the risk band to calibrate against survival:
    # GREEN tokens have high survival probability, BLACK tokens have near-zero.
    # This replaces the old feature-availability formula which was uncalibrated.
    from scoring.band_calibration import (
        band_confidence_map,
        band_confidence_source,
        phase_confidence_map,
    )

    confidence_source = band_confidence_source()
    # Phase-conditioned calibration (v4, 2026-11-20): when a phase map is
    # installed, confidence is the calibrated survival probability for the
    # (risk band, lifecycle phase at score time) cell, fit out-of-sample on
    # the v3_bounded reference set. Evidence completeness (availability,
    # coverage, flags, data-layer uncertainty) is not a survival signal and
    # no longer erodes confidence — it is reported as uncertainty instead.
    # Without a phase map (or a missing lifecycle_phase feature), the
    # band-level map applies exactly as before.
    phase_map = phase_confidence_map()
    phase_rank: int | None = None
    if phase_map is not None:
        try:
            phase_value = features.get("lifecycle_phase")
            phase_rank = int(float(phase_value)) if phase_value is not None else None
        except (TypeError, ValueError):
            phase_rank = None
    phase_calibrated = phase_map is not None and phase_rank is not None
    if phase_map is not None and phase_rank is not None:
        raw_confidence = phase_map.get(
            (risk_assessment.band, phase_rank),
            band_confidence_map().get(risk_assessment.band, 50.0),
        )
        confidence_source = "phase-calibrated"
    else:
        raw_confidence = band_confidence_map().get(risk_assessment.band, 50.0)
    confidence = clamp(raw_confidence)
    if session is not None:
        from common.config import get_settings
        from risk_engine.calibrator import get_active_confidence_calibration
        from risk_engine.confidence_calibration import apply_isotonic_map

        calibration = get_active_confidence_calibration(session)
        if (
            calibration is not None
            and calibration.sample_size >= get_settings().risk_confidence_calibration_min_samples
        ):
            confidence = apply_isotonic_map(confidence, calibration.x_points, calibration.y_points)
            confidence_source = "calibrated"
    # Risk calibration (v6, 2026-11-20): when the phase map drives
    # confidence, the risk score is the calibrated collapse probability —
    # the complement of the calibrated survival probability — fit
    # out-of-sample on the v3_bounded reference set. The raw rule-engine
    # score is badly calibrated as a collapse probability (mean
    # |risk/100 - collapsed| = 0.5026 on the v5 eval set; within-phase it
    # is inverted — lower risk, higher collapse in phase 1), and its
    # isotonic recalibration generalises worse than the raw score
    # (mean error 0.7085 out-of-sample on the same partition), so the
    # phase-conditioned rate is the calibration. Without a phase map the
    # raw rule score is kept (no invented numbers).
    # risk_assessment.score still feeds exit_risk / research_priority:
    # those are composites that use the raw rule score as one component,
    # not collapse probabilities.
    if phase_calibrated:
        risk_value = clamp(100.0 - confidence)
    else:
        risk_value = risk_assessment.score
    # Uncertainty is the predictive entropy of the calibrated survival
    # probability (v4b, 2026-11-20): a binary prediction is most uncertain
    # near p=0.5 and most certain near p=0 or p=1. The v4 evidence-based
    # uncertainty was wrong for this data — missingness is anti-correlated
    # with error here (dead tokens have the sparsest data and the most
    # predictable outcomes), and it drove the harness uncertainty
    # error-correlation to -0.48, worse than the -0.37 it replaced.
    # Data-layer degradation still widens uncertainty: unreliable features
    # genuinely increase the chance the prediction is wrong (see
    # test_rpc_data_layer_degradation_widens_uncertainty).
    _p = min(0.99, max(0.01, confidence / 100.0))
    _entropy = -(_p * math.log(_p) + (1.0 - _p) * math.log(1.0 - _p)) / math.log(2.0)
    uncertainty = clamp(100.0 * _entropy + data_layer_uncertainty)
    # Weighted average of the catalyst components (weights sum to 1.25, so the
    # sum is divided by 1.25) — keeps catalyst on the 0-100 scale instead of
    # inflating it ~25% before the clamp.
    catalyst = clamp(
        (
            0.55 * narrative
            + 0.25 * website
            + 0.20 * github
            + 0.15 * clamp(100.0 - catalyst_proximity * 5.0)
            + 0.05 * clamp(star_velocity)
            + 0.05 * clamp(download_velocity)
        )
        / 1.25
    )
    phase_penalty = clamp((lifecycle_phase - 2.0) * 25.0)  # parabolic=0, collapse=50
    # Weighted average of the exit-risk components (weights sum to 1.90, so the
    # sum is divided by 1.90) — keeps exit_risk on the 0-100 scale instead of
    # nearly doubling moderate risk before the clamp.
    exit_risk = clamp(
        (
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
        / 1.90
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
        risk=round(risk_value, 4),
        liquidity_access=round(liquidity_access, 4),
        manipulation=round(manipulation, 4),
        confidence=round(confidence, 4),
        uncertainty=round(uncertainty, 4),
        catalyst=round(catalyst, 4),
        exit_risk=round(exit_risk, 4),
        research_priority=round(research_priority, 4),
        risk_band=risk_assessment.band,
        confidence_source=confidence_source,
        drivers=drivers,
        risk_reasons=risk_assessment.reasons,
        missing_features=all_missing,
    )
