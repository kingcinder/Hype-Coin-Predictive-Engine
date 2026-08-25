from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from features.factory import FeatureFactory
from ingestion.rpc_pool import RpcEndpointPool
from risk_engine.rules import assess_risk, band_from_collapse_probability
from scoring.engine import ScoringEngine, score_current_assets
from scoring.formulas import compute_scores
from storage import models
from storage.repository import insert_liquidity_snapshot_once, insert_market_snapshot_once
from tests.conftest import seed_market_asset


def test_low_liquidity_black_hard_reject_overrides_hype() -> None:
    features = {
        "five_min_return": 80.0,
        "one_hour_return": 150.0,
        "volume_acceleration": 10.0,
        "liquidity_depth": 1000.0,
        "liquidity_change": 25.0,
        "buy_sell_ratio": 4.0,
        "unique_buyers_estimate": 100.0,
        "pair_age_minutes": 5.0,
        "holder_count": 20.0,
        "holder_growth": 10.0,
        "top_holder_concentration": 0.1,
        "spread_estimate": 5.0,
        "volatility": 10.0,
        "venue_agreement": 100.0,
        "mention_velocity": 50.0,
        "website_presence": 1.0,
        "github_presence_public": 1.0,
        "suspicious_contract_flags": 0.0,
        "deployer_history_available": 0.0,
        "narrative_acceleration": 3.0,
    }
    result = compute_scores(features, [])
    assert result.risk_band.value == "BLACK"
    assert result.research_priority == 0.0


def test_rpc_data_layer_degradation_widens_uncertainty() -> None:
    healthy = compute_scores({}, [])
    degraded = compute_scores({}, [], data_layer_uncertainty=35.0)
    assert degraded.confidence < healthy.confidence
    assert degraded.uncertainty > healthy.uncertainty
    assert degraded.drivers["rpc_pool_uncertainty"] == 35.0


def test_rpc_pool_health_is_a_persisted_feature_and_score_driver(session) -> None:
    """rpc_pool_health reads persisted RpcPoolSnapshot rows (point-in-time),
    never the live in-process pool — a historical feature snapshot must not
    leak current process memory into the past."""
    asset = seed_market_asset(session)
    chain = session.get(models.Chain, asset.chain_id)
    decision = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    session.add_all(
        [
            models.RpcPoolSnapshot(
                chain_slug=chain.slug,
                url="https://rpc-healthy.example.com",
                ts=decision,
                health=1.0,
                consecutive_failures=0,
                down=False,
                probe_count=5,
                probe_successes=5,
                probe_failures=0,
            ),
            models.RpcPoolSnapshot(
                chain_slug=chain.slug,
                url="https://rpc-down.example.com",
                ts=decision,
                health=0.0,
                consecutive_failures=2,
                down=True,
                probe_count=5,
                probe_successes=3,
                probe_failures=2,
            ),
        ]
    )
    session.commit()
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, decision)
    }
    assert values["rpc_pool_health"].value == 0.5
    assert values["rpc_pool_health"].missing is False

    result = compute_scores({"rpc_pool_health": values["rpc_pool_health"].value}, [])
    assert result.drivers["rpc_pool_health"] == 50.0


def test_website_presence_is_evidence_gated(session) -> None:
    """website_presence/github_presence_public must not read live asset state.

    A URL on the asset row reflects *current* discovery — for a historical
    decision the URL may not have been known yet.  Presence is only 1.0 when
    crawler evidence (social mention raw_ref or raw-evidence payload)
    referencing the URL was observed at or before the decision time.
    """
    asset = seed_market_asset(session)
    decision = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # Asset row has website_url/github_url, but no evidence observed before
    # the decision -> both read as absent (no live-state leak).
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, decision)
    }
    assert values["website_presence"].value == 0.0
    assert values["github_presence_public"].value == 0.0

    # Evidence observed at the decision time flips website presence on.
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    session.add(
        models.SocialMention(
            asset_id=asset.id,
            topic=asset.symbol,
            source_id=source.id,
            ts=decision,
            observed_at=decision,
            raw_ref="https://example.org/announcement",
            metrics_json={},
        )
    )
    session.commit()
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, decision)
    }
    assert values["website_presence"].value == 1.0
    # github still absent — no evidence referencing the github URL yet.
    assert values["github_presence_public"].value == 0.0


def test_website_presence_ignores_future_evidence(session) -> None:
    """Evidence observed AFTER the decision time must not count (no lookahead)."""
    asset = seed_market_asset(session)
    decision = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    session.add(
        models.SocialMention(
            asset_id=asset.id,
            topic=asset.symbol,
            source_id=source.id,
            ts=decision + timedelta(hours=2),
            observed_at=decision + timedelta(hours=2),
            raw_ref="https://example.org/announcement",
            metrics_json={},
        )
    )
    session.commit()
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, decision)
    }
    assert values["website_presence"].value == 0.0


def test_rpc_pool_health_neutral_without_snapshots(session) -> None:
    """No persisted RpcPoolSnapshot before the decision time -> neutral 1.0,
    not a fabricated 0.0 from an absent live pool."""
    asset = seed_market_asset(session)
    values = {
        value.name: value
        for value in FeatureFactory().build_for_asset(session, asset, datetime.now(UTC))
    }
    assert values["rpc_pool_health"].value == 1.0


def test_scoring_engine_reads_the_asset_chain_rpc_pool(session, monkeypatch) -> None:
    asset = seed_market_asset(session)
    pool = RpcEndpointPool(
        ["https://rpc-healthy.example.com", "https://rpc-down.example.com"],
        failure_threshold=1,
        chain_slug="solana",
    )
    pool.mark_failure("https://rpc-down.example.com")
    monkeypatch.setattr("scoring.engine.get_rpc_pool", lambda _chain: pool)
    assert ScoringEngine()._rpc_pool_uncertainty(session, asset.id) == 50.0


def test_extreme_holder_concentration_is_black() -> None:
    assessment = assess_risk(
        {
            "liquidity_depth": 200_000.0,
            "top_holder_concentration": 0.95,
            "pair_age_minutes": 120.0,
            "suspicious_contract_flags": 0.0,
        }
    )
    assert assessment.band.value == "BLACK"
    assert assessment.hard_reject is True


def test_liquidity_withdrawal_signal_raises_risk() -> None:
    assessment = assess_risk(
        {
            "liquidity_depth": 200_000.0,
            "top_holder_concentration": 0.1,
            "pair_age_minutes": 120.0,
            "suspicious_contract_flags": 0.0,
            "liquidity_withdrawal_signal": 1.0,
        }
    )
    assert assessment.band.value == "YELLOW"
    assert any("withdrawal" in reason for reason in assessment.reasons)


def test_withdrawal_on_shallow_book_is_hard_reject() -> None:
    assessment = assess_risk(
        {
            "liquidity_depth": 30_000.0,
            "top_holder_concentration": 0.1,
            "pair_age_minutes": 120.0,
            "suspicious_contract_flags": 0.0,
            "liquidity_withdrawal_signal": 1.0,
        }
    )
    assert assessment.hard_reject is True
    assert assessment.band.value == "BLACK"


def test_recidivism_overlap_raises_risk() -> None:
    assessment = assess_risk(
        {
            "liquidity_depth": 200_000.0,
            "top_holder_concentration": 0.1,
            "pair_age_minutes": 120.0,
            "suspicious_contract_flags": 0.0,
            "recidivism_score": 70.0,
        }
    )
    assert any("recidivism" in reason for reason in assessment.reasons)


def test_score_explanation_records_changed_features(session) -> None:
    asset = seed_market_asset(session)
    first_decision = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    score_current_assets(session, decision_ts=first_decision, asset_ids=[asset.id])
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    pool = session.scalar(select(models.Pool).where(models.Pool.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert pair is not None
    assert pool is not None
    assert source is not None

    second_decision = first_decision + timedelta(hours=1)
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=second_decision,
        observed_at=second_decision,
        price_usd=1.75,
        volume_usd=95_000,
        buys=80,
        sells=8,
        trades=88,
    )
    insert_liquidity_snapshot_once(
        session,
        pool_id=pool.id,
        source_id=source.id,
        ts=second_decision,
        observed_at=second_decision,
        reserve_usd=160_000,
    )
    scores = score_current_assets(session, decision_ts=second_decision, asset_ids=[asset.id])
    explanation = session.scalar(
        select(models.ScoreExplanation).where(models.ScoreExplanation.score_id == scores[0].id)
    )
    assert explanation is not None
    assert "liquidity_depth" in explanation.changed_features
    assert explanation.changed_features["liquidity_depth"]["current"] == 160_000.0


def test_band_from_collapse_probability_thresholds() -> None:
    """Verify band_from_collapse_probability maps probabilities to bands correctly."""
    # Below all thresholds → GREEN
    assert band_from_collapse_probability(0.0).value == "GREEN"
    assert band_from_collapse_probability(0.05).value == "GREEN"
    assert band_from_collapse_probability(0.0999).value == "GREEN"
    # 0.10+ → YELLOW
    assert band_from_collapse_probability(0.10).value == "YELLOW"
    assert band_from_collapse_probability(0.20).value == "YELLOW"
    assert band_from_collapse_probability(0.2999).value == "YELLOW"
    # 0.30+ → ORANGE
    assert band_from_collapse_probability(0.30).value == "ORANGE"
    assert band_from_collapse_probability(0.40).value == "ORANGE"
    assert band_from_collapse_probability(0.4999).value == "ORANGE"
    # 0.50+ → RED
    assert band_from_collapse_probability(0.50).value == "RED"
    assert band_from_collapse_probability(0.60).value == "RED"
    assert band_from_collapse_probability(0.7499).value == "RED"
    # 0.75+ → BLACK
    assert band_from_collapse_probability(0.75).value == "BLACK"
    assert band_from_collapse_probability(0.90).value == "BLACK"
    assert band_from_collapse_probability(1.0).value == "BLACK"


def _seed_calibration(
    session,
    *,
    yellow: float = 25.0,
    orange: float = 50.0,
    red: float = 75.0,
    ml_yellow: float = 0.10,
    ml_orange: float = 0.30,
    ml_red: float = 0.50,
) -> models.RiskCalibration:
    """Insert an active RiskCalibration row (used to test adaptive banding).

    Deactivates any previously-active row first (mirroring
    ``run_calibration``) so repeated seeding in one test stays deterministic
    for ``get_current_*_thresholds``, which selects the latest active row.
    """
    prev = session.scalar(
        select(models.RiskCalibration).where(models.RiskCalibration.active.is_(True))
    )
    if prev is not None:
        prev.active = False
    cal = models.RiskCalibration(
        version="test-cal",
        calibrated_at=datetime.now(UTC),
        sample_size=100,
        yellow_threshold=yellow,
        orange_threshold=orange,
        red_threshold=red,
        ml_yellow_threshold=ml_yellow,
        ml_orange_threshold=ml_orange,
        ml_red_threshold=ml_red,
        reason_weights={},
        band_precisions={},
        ml_band_precisions={},
        active=True,
    )
    session.add(cal)
    session.commit()
    return cal


def test_band_from_collapse_probability_defaults_without_calibration(session) -> None:
    """With no RiskCalibration row, a session yields the hardcoded defaults."""
    assert band_from_collapse_probability(0.10, session=session).value == "YELLOW"
    assert band_from_collapse_probability(0.30, session=session).value == "ORANGE"
    assert band_from_collapse_probability(0.50, session=session).value == "RED"
    assert band_from_collapse_probability(0.7499, session=session).value == "RED"
    assert band_from_collapse_probability(0.75, session=session).value == "BLACK"


def test_band_from_collapse_probability_uses_ml_thresholds(session) -> None:
    """The ML band mapping uses ML-specific probability thresholds directly.

    These are learned from ML scorer outcomes (not bridged from rule-engine
    score thresholds): seeding ml thresholds 0.22/0.38/0.54 shifts the bands
    exactly, while the rule-engine score thresholds are irrelevant to the
    ML mapping.
    """
    _seed_calibration(
        session,
        yellow=40.0,
        orange=60.0,
        red=80.0,
        ml_yellow=0.22,
        ml_orange=0.38,
        ml_red=0.54,
    )

    # YELLOW starts at 0.22 instead of 0.10
    assert band_from_collapse_probability(0.10, session=session).value == "GREEN"
    assert band_from_collapse_probability(0.21, session=session).value == "GREEN"
    assert band_from_collapse_probability(0.22, session=session).value == "YELLOW"
    # ORANGE at 0.38
    assert band_from_collapse_probability(0.30, session=session).value == "YELLOW"
    assert band_from_collapse_probability(0.37, session=session).value == "YELLOW"
    assert band_from_collapse_probability(0.38, session=session).value == "ORANGE"
    # RED at 0.54
    assert band_from_collapse_probability(0.50, session=session).value == "ORANGE"
    assert band_from_collapse_probability(0.53, session=session).value == "ORANGE"
    assert band_from_collapse_probability(0.54, session=session).value == "RED"
    # BLACK stays fixed at 0.75
    assert band_from_collapse_probability(0.60, session=session).value == "RED"
    assert band_from_collapse_probability(0.7499, session=session).value == "RED"
    assert band_from_collapse_probability(0.75, session=session).value == "BLACK"


def _collapse_features(prob: float) -> dict[str, float]:
    """Base feature set for testing the collapse-probability rule contribution."""
    return {
        "liquidity_depth": 200_000.0,
        "top_holder_concentration": 0.1,
        "pair_age_minutes": 120.0,
        "suspicious_contract_flags": 0.0,
        "collapse_probability_24h": prob,
    }


def test_collapse_probability_contribution_follows_default_band_boundaries() -> None:
    """Without calibration the collapse contribution graduates with the ML band.

    ORANGE (>= default 0.30) adds a smaller elevated-collapse amount; RED
    (>= default 0.50) and BLACK (>= 0.75) add the full amount — the same
    boundaries the ML band mapping uses instead of a hardcoded 0.6 that
    matched no band.
    """
    below = assess_risk(_collapse_features(0.29))
    assert not any("collapse probability" in r.lower() for r in below.reasons)
    at_orange = assess_risk(_collapse_features(0.30))
    assert any("collapse probability" in r.lower() for r in at_orange.reasons)
    assert at_orange.score == pytest.approx(10.0)
    # Old hardcoded behavior: 0.55 used to be below the 0.6 trigger even
    # though the ML band mapping already called it RED.  Now it contributes.
    in_red = assess_risk(_collapse_features(0.55))
    assert any("collapse probability" in r.lower() for r in in_red.reasons)
    assert in_red.score == pytest.approx(20.0)
    black = assess_risk(_collapse_features(0.80))
    assert any("collapse probability" in r.lower() for r in black.reasons)


def test_collapse_probability_contribution_uses_calibrated_ml_red_threshold(session) -> None:
    """A calibrated ML red threshold shifts the RED collapse contribution with it.

    The ORANGE tier keeps graduating on the *default* orange boundary (0.30)
    until it is calibrated too, so a calibrated red of 0.54 first raises the
    elevated 10pt ORANGE contribution, then the full 20pt RED contribution at
    the boundary itself.
    """
    _seed_calibration(session, ml_red=0.54)

    below = assess_risk(_collapse_features(0.29), session=session)
    assert not any("collapse probability" in r.lower() for r in below.reasons)
    elevated = assess_risk(_collapse_features(0.53), session=session)
    assert any("elevated" in r.lower() for r in elevated.reasons)
    assert elevated.score == pytest.approx(10.0)
    at_boundary = assess_risk(_collapse_features(0.54), session=session)
    assert any("collapse probability" in r.lower() for r in at_boundary.reasons)
    assert at_boundary.score == pytest.approx(20.0)
    # Calibrated red of 0.54 still leaves BLACK at the fixed 0.75.
    assert any(
        "collapse probability" in r.lower()
        for r in assess_risk(_collapse_features(0.75), session=session).reasons
    )


def test_rule_thresholds_do_not_shift_ml_bands(session) -> None:
    """Rule-engine score thresholds alone never move the ML band mapping.

    The ML scorer calibrates on its own signal: a calibration row that only
    moved the rule thresholds (leaving ml thresholds at defaults) must keep
    the ML mapping at the default probability boundaries.
    """
    _seed_calibration(
        session,
        yellow=40.0,
        orange=60.0,
        red=80.0,
        ml_yellow=0.10,
        ml_orange=0.30,
        ml_red=0.50,
    )
    assert band_from_collapse_probability(0.10, session=session).value == "YELLOW"
    assert band_from_collapse_probability(0.30, session=session).value == "ORANGE"
    assert band_from_collapse_probability(0.50, session=session).value == "RED"
    assert band_from_collapse_probability(0.75, session=session).value == "BLACK"


def test_band_from_collapse_probability_relaxed_ml_thresholds(session) -> None:
    """Relaxed ML probability thresholds loosen the ML bands."""
    _seed_calibration(
        session,
        ml_yellow=0.06,
        ml_orange=0.26,
        ml_red=0.46,
    )
    assert band_from_collapse_probability(0.05, session=session).value == "GREEN"
    assert band_from_collapse_probability(0.06, session=session).value == "YELLOW"
    assert band_from_collapse_probability(0.26, session=session).value == "ORANGE"
    assert band_from_collapse_probability(0.46, session=session).value == "RED"


def test_red_never_exceeds_black_across_calibrated_thresholds(session) -> None:
    """RED can never exceed BLACK (prob 0.75) for any calibrated threshold combo.

    Sweeps a bounded set of adversarial ML probability threshold triples —
    defaults, degenerate collisions, inverted orderings, and values at/above
    the BLACK boundary — asserting the monotonicity and band-collapse
    invariants: the BLACK boundary stays fixed at 0.75 and is checked first,
    nothing below 0.75 is ever BLACK, and the YELLOW/ORANGE/RED chain is
    clamped into a strictly-ordered sequence so no band can swallow its
    neighbor even with a wildly drifted calibration row.

    (Rule-engine score thresholds are deliberately not swept here: they never
    influence ``band_from_collapse_probability``, which maps on the ML
    probability scale only.)
    """
    # (ml_yellow, ml_orange, ml_red) adversarial triples covering the clamp
    # and ordering edge cases in band_from_collapse_probability.
    adversarial = [
        (0.10, 0.30, 0.50),  # defaults
        (0.05, 0.26, 0.46),  # relaxed bands
        (0.22, 0.38, 0.54),  # tightened bands
        (0.10, 0.10, 0.50),  # yellow == orange collision
        (0.10, 0.30, 0.30),  # orange == red collision
        (0.30, 0.10, 0.50),  # inverted yellow/orange
        (0.74, 0.74, 0.74),  # everything at the RED clamp ceiling
        (0.75, 0.75, 0.75),  # everything at/above BLACK -> clamped below
        (0.90, 0.95, 1.00),  # everything above BLACK
        (0.00, 0.00, 0.00),  # everything at zero
        (0.01, 0.02, 0.74),  # narrow chain just under BLACK
    ]
    prob_samples = (0.0, 0.10, 0.30, 0.50, 0.70, 0.74, 0.7499, 0.75, 0.80, 0.99)
    for ml_yellow, ml_orange, ml_red in adversarial:
        _seed_calibration(
            session,
            ml_yellow=ml_yellow,
            ml_orange=ml_orange,
            ml_red=ml_red,
        )
        # BLACK is always 0.75 and checked first.
        assert band_from_collapse_probability(0.75, session=session).value == "BLACK"
        assert band_from_collapse_probability(0.80, session=session).value == "BLACK"
        # Nothing below 0.75 can ever be BLACK.
        for p in prob_samples:
            if p < 0.75:
                assert band_from_collapse_probability(p, session=session).value != "BLACK", (
                    f"p={p} mapped to BLACK at thresholds Y={ml_yellow} O={ml_orange} R={ml_red}"
                )
        # The RED boundary is clamped to at most 0.74, so RED stays
        # reachable strictly below BLACK (no band collapse).
        effective_red = min(ml_red, 0.74)
        assert band_from_collapse_probability(effective_red, session=session).value == "RED"


def _seed_evaluated_ml_outcomes(
    session,
    asset: models.Asset,
    *,
    ml_band: str,
    total: int,
    collapsed: int,
) -> None:
    """Insert evaluated RiskOutcome rows carrying ML-specific predictions."""
    from risk_engine.outcomes import record_risk_outcome

    now = datetime.now(UTC)
    decision = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    score_current_assets(session, decision_ts=decision, asset_ids=[asset.id])
    base_score = session.scalar(select(models.Score).where(models.Score.asset_id == asset.id))
    for index in range(total):
        score_ts = decision + timedelta(minutes=index + 1)
        score_current_assets(session, decision_ts=score_ts, asset_ids=[asset.id])
        score = session.scalar(
            select(models.Score).where(
                models.Score.asset_id == asset.id,
                models.Score.decision_ts == score_ts,
            )
        )
        record_risk_outcome(
            session,
            asset_id=asset.id,
            risk_band="RED",
            score_id=score.id if score else base_score.id,
            decision_ts=score_ts,
            ml_risk_band=ml_band,
        )
        target_score_id = score.id if score else base_score.id
        outcome = session.scalar(
            select(models.RiskOutcome).where(models.RiskOutcome.score_id == target_score_id)
        )
        outcome.evaluated_at = now
        outcome.collapsed = index < collapsed
        outcome.lifecycle_phase_at_eval = "collapse" if index < collapsed else "survivor"
    session.commit()


def test_run_calibration_learns_ml_thresholds_from_ml_outcomes(session) -> None:
    """run_calibration learns ML probability thresholds from ML outcomes.

    10 RED ML predictions with 7 collapses (precision 0.7) tighten the ML
    red threshold above its 0.50 default, persist it, and feed it back
    through get_current_ml_thresholds/band_from_collapse_probability.
    """
    from risk_engine.calibrator import get_current_ml_thresholds, run_calibration

    asset = seed_market_asset(session)
    _seed_evaluated_ml_outcomes(session, asset, ml_band="RED", total=10, collapsed=7)

    result = run_calibration(session)
    session.commit()

    # High precision (0.7) tightens the ML red boundary above the default.
    assert result.ml_adjusted is True
    assert result.ml_red_threshold > 0.50
    assert result.ml_band_precisions.get("RED", 0.0) >= 0.6

    ml_yellow, ml_orange, ml_red = get_current_ml_thresholds(session)
    assert ml_yellow == 0.10  # no YELLOW ML samples -> default
    assert ml_orange == 0.30  # no ORANGE ML samples -> default
    assert ml_red == result.ml_red_threshold
    assert band_from_collapse_probability(ml_red, session=session).value == "RED"
    assert band_from_collapse_probability(ml_red - 0.001, session=session).value == "ORANGE"


def test_ml_band_precisions_in_evaluate_outcomes(session) -> None:
    """evaluate_outcomes reports ML-band outcomes separately from rule bands."""
    from risk_engine.outcomes import evaluate_outcomes

    asset = seed_market_asset(session)
    _seed_evaluated_ml_outcomes(session, asset, ml_band="ORANGE", total=8, collapsed=2)

    report = evaluate_outcomes(session)
    orange = report.ml_bands.get("ORANGE")
    assert orange is not None
    assert orange.total_flagged == 8
    assert orange.collapsed == 2
    assert abs(orange.precision - 0.25) < 1e-9
    # The rule band for these rows is RED (seeded risk_band), so ML and rule
    # bands are tracked independently.
    assert report.bands.get("RED", None) is not None
