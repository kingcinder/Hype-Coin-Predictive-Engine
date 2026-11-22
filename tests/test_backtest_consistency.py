"""Backtest/replay scoring-path consistency: the replay must score with the
same missing-feature convention as the engine and the rescore.

Regression test for reports/backtest-consistency-20261121.md.
``backtest.runner._score_replay_candidates`` used to build ``raw`` from
EVERY stored feature value, including mf=1 placeholders (0.0) for missing
features, while ``scoring.engine.score_assets`` (fixed 2026-11-20, see
reports/scoring-path-consistency-fix-20261120.md) and
``scripts/rescore.py`` build ``raw`` from non-missing features only, so a
missing feature falls back to its formula default. ``compute_scores``
strips the missing keys for ``assess_risk`` only; its body components read
the full dict, so the old replay convention scored a missing
``spread_estimate`` as a perfect spread and a missing
``catalyst_proximity_hours`` as "catalyst right now":
  exit_risk understated by exactly 15/1.90 = 7.8947,
  catalyst overstated by exactly 12.0,
  liquidity_access overstated by exactly 25.0.
Only ``research_priority`` feeds replay selection (top-k by research
priority, BLACK filtered on the unchanged risk band), and it inherits the
liquidity_access overstatement, so spread-missing tokens ranked too high
in backtests. The fix builds ``raw`` from non-missing features only in
``_score_replay_candidates``, exactly like the engine.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backtest.runner import ReplayCandidate, _score_replay_candidates
from features.factory import FeatureValue
from ops.score_drift import engine_score
from scoring.formulas import compute_scores
from storage.database import Base

DECISION_TS = datetime(2026, 11, 20, tzinfo=UTC)

# Healthy non-missing features shared by both fixture tokens. These are
# tuned (unlike the sparse fixture in test_scoring_path_consistency.py)
# so research_priority is positive with headroom: the liquidity_access
# overstatement must actually move the ranking signal instead of dying
# in the clamp at 0.
HEALTHY_FEATURES = {
    "liquidity_depth": 1_000_000.0,
    "pair_age_minutes": 120.0,
    "buy_sell_ratio": 1.5,
    "volatility": 5.0,
    "top_holder_concentration": 0.1,
    "five_min_return": 10.0,
    "one_hour_return": 8.0,
    "unique_buyers_estimate": 50.0,
    "mention_velocity": 10.0,
    "volume_acceleration": 3.0,
    "liquidity_change": 5.0,
    "venue_agreement": 90.0,
    "ignition_signal": 0.8,
    "website_presence": 1.0,
    "github_presence_public": 1.0,
    "holder_growth": 5.0,
    "narrative_acceleration": 2.0,
}

# The three features whose defaults differ from the 0.0 placeholder the
# factory stores for a missing value (see scoring/formulas.py):
#   spread_estimate:         default 100.0 (worst-case) vs placeholder 0.0
#   catalyst_proximity_hours: default 168.0 (no catalyst) vs placeholder 0.0
#   lifecycle_phase:         default 1.0 vs placeholder 0.0
MISSING_FEATURES = {
    "spread_estimate": 0.0,
    "catalyst_proximity_hours": 0.0,
    "lifecycle_phase": 0.0,
}

# The same three features with real, non-missing values (control token).
PRESENT_FEATURES = {
    "spread_estimate": 15.0,
    "catalyst_proximity_hours": 168.0,
    "lifecycle_phase": 1.0,
}


def _feature_map(
    healthy: dict[str, float],
    extra: dict[str, float],
    *,
    extra_missing: bool,
) -> dict[str, FeatureValue]:
    values = dict(healthy)
    values.update(extra)
    return {
        name: FeatureValue(
            name=name,
            value=value,
            missing=extra_missing and name in extra,
        )
        for name, value in values.items()
    }


def _stripped(feature_map: dict[str, FeatureValue]) -> tuple[dict[str, float], list[str]]:
    """The engine/rescore convention: non-missing values only, plus the missing list."""
    features = {name: feature.value for name, feature in feature_map.items() if not feature.missing}
    missing = [name for name, feature in feature_map.items() if feature.missing]
    return features, missing


def _all_values(feature_map: dict[str, FeatureValue]) -> tuple[dict[str, float], list[str]]:
    """The OLD replay convention: every stored value, including mf=1 0.0s."""
    features = {name: feature.value for name, feature in feature_map.items()}
    missing = [name for name, feature in feature_map.items() if feature.missing]
    return features, missing


def _replay_one(feature_map: dict[str, FeatureValue]) -> ReplayCandidate:
    candidates = _score_replay_candidates({1: feature_map}, decision_ts=DECISION_TS)
    assert len(candidates) == 1
    return candidates[0]


class TestBacktestConsistency:
    """_score_replay_candidates must agree with the engine/rescore convention."""

    def test_replay_matches_engine_convention(self) -> None:
        """Replay risk_band/research_priority equal compute_scores on stripped raw.

        The oracle is the exact call ``score_assets`` makes (modulo the
        session, which the replay deliberately passes as None, and the
        forecast mask, a no-op here because the fixture carries no
        collapse_probability_24h). Covers the token with missing features
        (the regression) and the control token with everything present.
        """
        for extra, extra_missing in ((MISSING_FEATURES, True), (PRESENT_FEATURES, False)):
            feature_map = _feature_map(HEALTHY_FEATURES, extra, extra_missing=extra_missing)
            features, missing = _stripped(feature_map)
            expected = compute_scores(features, missing, session=None)
            got = _replay_one(feature_map)
            assert got.risk_band == expected.risk_band.value
            assert got.research_priority == pytest.approx(expected.research_priority)

    def test_replay_matches_rescore_rule_result(self) -> None:
        """Replay also equals the rescore path's rule result field for field.

        ``ops.score_drift.engine_score`` reproduces what the rescore would
        write; its ``rule_result`` is the unblended rule score, which is
        what the replay measures. The ensemble blend cannot move the rule
        result, so no weight pinning is needed here.
        """
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            for extra, extra_missing in ((MISSING_FEATURES, True), (PRESENT_FEATURES, False)):
                feature_map = _feature_map(HEALTHY_FEATURES, extra, extra_missing=extra_missing)
                features, missing = _stripped(feature_map)
                expected = engine_score(session, features, missing, masked=False).rule_result
                got = _replay_one(feature_map)
                assert got.risk_band == expected.risk_band.value
                assert got.research_priority == pytest.approx(expected.research_priority)
        finally:
            session.close()

    def test_old_convention_differs_by_exact_fingerprint(self) -> None:
        """The pre-fix replay convention scored provably different values.

        Independent of the runner: direct ``compute_scores`` calls under
        both conventions on the same feature map. On the token with missing
        features the old raw (mf=1 0.0s included) understates exit_risk by
        7.8947, overstates catalyst by 12.0 and liquidity_access by 25.0,
        and inflates research_priority by exactly
        hype * (confidence / 100) * (25 / 100) through the liquidity_access
        term -- the channel that moved backtest top-k selection. On the
        control token the conventions agree exactly.
        """
        for extra, extra_missing, is_missing_token in (
            (MISSING_FEATURES, True, True),
            (PRESENT_FEATURES, False, False),
        ):
            feature_map = _feature_map(HEALTHY_FEATURES, extra, extra_missing=extra_missing)
            old_features, missing = _all_values(feature_map)
            new_features, _ = _stripped(feature_map)
            old = compute_scores(old_features, missing, session=None)
            new = compute_scores(new_features, missing, session=None)
            if is_missing_token:
                assert new.exit_risk - old.exit_risk == pytest.approx(7.8947, abs=1e-3)
                assert old.catalyst - new.catalyst == pytest.approx(12.0, abs=1e-3)
                assert old.liquidity_access - new.liquidity_access == pytest.approx(25.0, abs=1e-3)
                expected_rp_delta = new.hype * (new.confidence / 100.0) * 0.25
                assert old.research_priority - new.research_priority == pytest.approx(
                    expected_rp_delta, abs=1e-3
                )
                assert old.risk_band == new.risk_band  # assess_risk already strips
            else:
                assert old.exit_risk == pytest.approx(new.exit_risk)
                assert old.catalyst == pytest.approx(new.catalyst)
                assert old.liquidity_access == pytest.approx(new.liquidity_access)
                assert old.research_priority == pytest.approx(new.research_priority)
                assert old.risk_band == new.risk_band

    def test_missing_feature_is_indistinguishable_from_absent(self) -> None:
        """After the fix, a missing feature scores exactly like an absent one.

        The replay's ``raw`` must not contain a missing feature at all, so
        deleting the mf=1 keys from the map changes nothing. This is the
        replay-level statement of the fix: it fails under the old
        convention (the 0.0 placeholders move research_priority) and passes
        under the new one.
        """
        feature_map = _feature_map(HEALTHY_FEATURES, MISSING_FEATURES, extra_missing=True)
        pruned = {
            name: feature for name, feature in feature_map.items() if name not in MISSING_FEATURES
        }
        got = _replay_one(feature_map)
        expected = _replay_one(pruned)
        assert got.risk_band == expected.risk_band
        assert got.research_priority == pytest.approx(expected.research_priority)
