"""Task 10: ``research_priority`` is an entry ranking (spec §3, §8).

The pre-reframe formula subtracted collapse probability, the rule risk
score, manipulation and uncertainty, and then zeroed BLACK-band assets
entirely — collapse-avoidance wearing an entry ranking's clothes. The
coin most likely to moon-and-die, the mission's exact target, ranked
last. The reframed score is driven by ignition probability, expected
gain, and lead time (hours to peak); collapse probability leaves the
entry ranking and stays where it belongs — position management, via
``exit_risk``, whose formula is byte-for-byte unchanged (golden-pinned
below).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.enums import RiskBand
from scoring.formulas import compute_scores
from validation.harness import EntryRow, evaluate_entry


def features(
    *,
    p_ignition: float | None = None,
    ignition_signal: float | None = None,
    collapse_probability: float = 0.0,
    expected_hours_to_peak: float | None = None,
    band: str | None = None,
) -> dict[str, float]:
    """Feature dict for ``compute_scores``.

    ``band`` is documentary: the final band is produced by the existing
    machinery (a 0.9 collapse probability floors the band to BLACK), so
    the pinning test asserts the band it expects instead of injecting
    one.
    """
    feats: dict[str, float] = {
        "five_min_return": 10.0,
        "one_hour_return": 25.0,
        "volume_acceleration": 3.0,
        "liquidity_depth": 200_000.0,
        "liquidity_change": 8.0,
        "buy_sell_ratio": 1.6,
        "unique_buyers_estimate": 50.0,
        "pair_age_minutes": 120.0,
        "holder_count": 300.0,
        "holder_growth": 5.0,
        "top_holder_concentration": 0.3,
        "spread_estimate": 10.0,
        "volatility": 18.0,
        "venue_agreement": 85.0,
        "mention_velocity": 8.0,
        "website_presence": 1.0,
        "github_presence_public": 0.0,
        "suspicious_contract_flags": 0.0,
        "narrative_acceleration": 1.5,
        "collapse_probability_24h": collapse_probability,
        "lifecycle_phase": 2.0,
        "catalyst_proximity_hours": 72.0,
    }
    if p_ignition is not None:
        feats["p_ignition_24h"] = p_ignition
    if ignition_signal is not None:
        feats["ignition_signal"] = ignition_signal
    if expected_hours_to_peak is not None:
        feats["expected_hours_to_peak"] = expected_hours_to_peak
    return feats


def test_high_collapse_high_ignition_coin_ranks_high() -> None:
    # The old formula zeroed this coin (BLACK band, collapse_probability .9).
    # Mission semantics: p_ignition .8 with a fast expected peak is a TOP
    # entry candidate; collapse risk is the exit policy's problem.
    result = compute_scores(
        features(
            p_ignition=0.8,
            collapse_probability=0.9,
            expected_hours_to_peak=3.0,
            band="BLACK",
        ),
        [],
    )
    assert result.risk_band == RiskBand.BLACK
    assert result.research_priority >= 60.0


def test_research_priority_monotone_in_p_ignition() -> None:
    low = compute_scores(
        features(p_ignition=0.2, collapse_probability=0.3, expected_hours_to_peak=6.0),
        [],
    )
    high = compute_scores(
        features(p_ignition=0.7, collapse_probability=0.3, expected_hours_to_peak=6.0),
        [],
    )
    assert high.research_priority > low.research_priority


def test_research_priority_ignores_collapse_probability() -> None:
    # Invariant: collapse probability is not an input to the entry
    # ranking at all — the same entry case scores identically whether
    # the coin is expected to survive or to die after the peak.
    calm = compute_scores(
        features(p_ignition=0.6, collapse_probability=0.0, expected_hours_to_peak=4.0),
        [],
    )
    doomed = compute_scores(
        features(p_ignition=0.6, collapse_probability=0.95, expected_hours_to_peak=4.0),
        [],
    )
    assert doomed.research_priority == pytest.approx(calm.research_priority)


def test_p_ignition_falls_back_to_ignition_signal() -> None:
    # scoring/engine.py does not (yet) pass the persisted forecast's
    # p_ignition_24h / expected_hours_to_peak through the feature dict;
    # until it does, the factory's ignition_signal feature carries the
    # ignition probability. 0.45*80 + 0.30*80 + 0.25*(100 - 3*100/24).
    result = compute_scores(
        features(ignition_signal=0.8, expected_hours_to_peak=3.0),
        [],
    )
    assert result.research_priority == pytest.approx(81.875)


def test_persisted_forecast_wins_over_ignition_signal() -> None:
    # When both are present, p_ignition_24h is the forecast's number and
    # wins: 0.45*10 + 0.30*10 + 0.25*(100 - 3*100/24).
    result = compute_scores(
        features(p_ignition=0.1, ignition_signal=0.9, expected_hours_to_peak=3.0),
        [],
    )
    assert result.research_priority == pytest.approx(29.375)


def test_missing_hours_to_peak_earns_no_lead_time_credit() -> None:
    # No forecast -> no lead-time claim: the term contributes 0, exactly
    # as if the peak were a full forward window away.
    # 0.45*80 + 0.30*80 + 0.
    result = compute_scores(features(p_ignition=0.8), [])
    assert result.research_priority == pytest.approx(60.0)


def test_drivers_explain_the_entry_rank() -> None:
    result = compute_scores(
        features(p_ignition=0.8, expected_hours_to_peak=3.0),
        [],
    )
    assert result.drivers["p_ignition_24h"] == pytest.approx(80.0)
    assert result.drivers["expected_hours_to_peak"] == pytest.approx(3.0)


# The fixed feature dict for the exit_risk golden test: exercises every
# exit-risk component (rule risk score, volatility, liquidity change,
# spread, withdrawal / LP-removal signals, recidivism, collapse
# probability, lifecycle phase penalty).
_GOLDEN_FEATURES: dict[str, float] = {
    "five_min_return": 12.0,
    "one_hour_return": 30.0,
    "volume_acceleration": 4.0,
    "liquidity_depth": 250000.0,
    "liquidity_change": -18.0,
    "buy_sell_ratio": 1.8,
    "unique_buyers_estimate": 60.0,
    "pair_age_minutes": 90.0,
    "holder_count": 400.0,
    "holder_growth": 6.0,
    "top_holder_concentration": 0.35,
    "spread_estimate": 8.0,
    "volatility": 22.0,
    "venue_agreement": 80.0,
    "mention_velocity": 12.0,
    "website_presence": 1.0,
    "github_presence_public": 0.0,
    "suspicious_contract_flags": 1.0,
    "narrative_acceleration": 2.0,
    "ignition_signal": 0.55,
    "liquidity_withdrawal_signal": 0.4,
    "lp_removal_signal": 0.2,
    "recidivism_score": 10.0,
    "collapse_probability_24h": 0.45,
    "lifecycle_phase": 3.0,
    "catalyst_proximity_hours": 48.0,
}


def test_exit_risk_formula_unchanged() -> None:
    # Golden value recorded from the pre-reframe code (Task 10 Step 3):
    # the reframe must not move exit_risk by a single rounding step.
    result = compute_scores(dict(_GOLDEN_FEATURES), [])
    assert result.exit_risk == pytest.approx(25.9737)


def test_entry_evaluator_cells_compute_against_reframed_priority() -> None:
    # Task 6 regression: the entry evaluator's entry_priority cells read
    # Score.research_priority. Feed them priorities produced by the
    # reframed formula itself — hot (high-ignition, fast peak) coins
    # realize the gains, cold coins do not — and the gain-lift cell must
    # compute and certify the ranking (top-decile hit rate 1.0 vs base
    # 0.5 -> lift 2.0).
    decision = datetime(2026, 11, 20, tzinfo=UTC)
    rows: list[EntryRow] = []
    for i in range(20):
        hot = compute_scores(
            features(p_ignition=0.8, expected_hours_to_peak=2.0),
            [],
        )
        rows.append(
            EntryRow(
                asset_id=1000 + i,
                decision_ts=decision,
                research_priority=hot.research_priority,
                p_ignition_24h=0.8,
                max_gain_pct=1.5,
                hours_to_peak=2.0,
                phase_at_score=None,
            )
        )
    for i in range(20):
        cold = compute_scores(
            features(p_ignition=0.05, expected_hours_to_peak=2.0),
            [],
        )
        rows.append(
            EntryRow(
                asset_id=2000 + i,
                decision_ts=decision,
                research_priority=cold.research_priority,
                p_ignition_24h=0.05,
                max_gain_pct=0.05,
                hours_to_peak=None,
                phase_at_score=None,
            )
        )
    cells = evaluate_entry(rows, regime="test", min_samples=10)
    by_key = {(c.output, c.metric): c for c in cells}
    lift = by_key[("entry_priority", "gain_lift_top_decile")]
    assert lift.value == pytest.approx(2.0)
    precision = by_key[("entry_priority", "precision_at_20")]
    assert precision.value == pytest.approx(1.0)
