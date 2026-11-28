"""Ticket 1 step 4b (M2): the ML band floors the final risk band.

Before this change the persisted band came only from assess_risk, where a
RED-band collapse probability contributes at most +20 points — so a clean
token with a 60% modeled collapse probability scored 20 and stayed GREEN.
A RED-band probability must not coexist with a GREEN verdict.
"""

from __future__ import annotations

from common.enums import RiskBand
from scoring.formulas import compute_scores


def _full_features(**overrides: float) -> dict[str, float]:
    features: dict[str, float] = {
        "five_min_return": 2.0,
        "one_hour_return": 5.0,
        "volume_acceleration": 1.5,
        "liquidity_depth": 250_000.0,
        "liquidity_change": 10.0,
        "buy_sell_ratio": 1.2,
        "unique_buyers_estimate": 400.0,
        "pair_age_minutes": 5_000.0,
        "holder_count": 3_000.0,
        "holder_growth": 2.0,
        "top_holder_concentration": 0.15,
        "spread_estimate": 2.0,
        "volatility": 12.0,
        "venue_agreement": 95.0,
        "drawdown_from_24h_peak": 0.05,
        "mention_velocity": 20.0,
        "website_presence": 1.0,
        "github_presence_public": 1.0,
        "suspicious_contract_flags": 0.0,
        "deployer_history_available": 1.0,
        "narrative_acceleration": 2.0,
        "ignition_signal": 0.0,
        "liquidity_withdrawal_signal": 0.0,
        "lp_removal_signal": 0.0,
        "recidivism_score": 0.0,
        "prelaunch_priority": 0.0,
        "catalyst_proximity_hours": 72.0,
        "narrative_cluster_growth_7d": 1.0,
        "shill_channel_diversity": 5.0,
        "prelaunch_narrative_velocity": 0.0,
        "kol_velocity": 3.0,
        "github_star_velocity": 1.0,
        "hf_download_velocity": 10.0,
        "rpc_pool_health": 1.0,
        "collapse_probability_24h": 0.02,
        "lifecycle_phase": 1.0,
        "solana_mint_authority_active": 0.0,
        "solana_freeze_authority_active": 0.0,
        "solana_supply": 1_000_000.0,
        "solana_decimals": 6.0,
        "solana_transfer_fee_enabled": 0.0,
        "solana_permanent_delegate_enabled": 0.0,
        "solana_non_transferable_enabled": 0.0,
        "solana_default_account_frozen": 0.0,
        "solana_lp_unlocked": 0.0,
    }
    features.update(overrides)
    return features


def test_red_probability_floors_green_verdict_to_red() -> None:
    result = compute_scores(_full_features(collapse_probability_24h=0.60), [])
    assert result.risk_band is RiskBand.RED
    assert any("floored" in reason for reason in result.risk_reasons)


def test_orange_probability_floors_green_verdict_to_orange() -> None:
    result = compute_scores(_full_features(collapse_probability_24h=0.35), [])
    assert result.risk_band is RiskBand.ORANGE


def test_black_probability_floors_to_black() -> None:
    result = compute_scores(_full_features(collapse_probability_24h=0.80), [])
    assert result.risk_band is RiskBand.BLACK


def test_low_probability_leaves_green_alone() -> None:
    result = compute_scores(_full_features(collapse_probability_24h=0.05), [])
    assert result.risk_band is RiskBand.GREEN
    assert not any("floored" in reason for reason in result.risk_reasons)


def test_missing_probability_applies_no_floor() -> None:
    features = _full_features()
    del features["collapse_probability_24h"]
    result = compute_scores(features, ["collapse_probability_24h"])
    assert result.risk_band is RiskBand.GREEN


def test_floor_never_lowers_a_worse_rule_band() -> None:
    # Hard-reject territory (extreme concentration) stays BLACK even with a
    # merely ORANGE probability: the floor only raises.
    result = compute_scores(
        _full_features(collapse_probability_24h=0.35, top_holder_concentration=0.95),
        [],
    )
    assert result.risk_band is RiskBand.BLACK
