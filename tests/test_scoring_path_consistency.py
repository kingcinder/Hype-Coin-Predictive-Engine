"""Scoring-path consistency: the engine and the rescore must write scores
with the same convention for missing features.

Regression test for reports/scoring-path-consistency-fix-20261120.md.
The engine used to pass mf=1 placeholder values (0.0) to compute_scores,
while the rescore drops mf=1 rows so missing features fall back to their
defaults (missing spread -> worst-case 100). A missing spread_estimate
stored as 0.0 therefore scored as a perfect spread, shifting exit_risk by
exactly 15/1.90 = 7.8947; a missing catalyst_proximity_hours stored as 0.0
scored as "catalyst right now" (+12.0 catalyst); liquidity_access was
+25.0 on the same rows. The fix builds `raw` from non-missing features
only in scoring.engine.ScoringEngine.score_assets, so the engine now
agrees with scripts/rescore.py `_build_features` / ops.score_drift
`engine_score` on every persisted field.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from features.factory import FeatureValue
from ops.score_drift import engine_score
from scoring import engine as engine_mod
from scoring.engine import ScoringEngine
from scoring.formulas import compute_scores
from storage import models
from storage.database import Base

DECISION_TS = datetime(2026, 11, 20, tzinfo=UTC)

# Healthy non-missing features shared by both fixture tokens.
HEALTHY_FEATURES = {
    "liquidity_depth": 5000.0,
    "pair_age_minutes": 2.0,
    "buy_sell_ratio": 0.2,
    "volatility": 40.0,
    "top_holder_concentration": 0.7,
}

# The three features whose defaults differ from the 0.0 placeholder the
# factory stores for a missing value (see scoring/formulas.py):
#   spread_estimate:        default 100.0 (worst-case) vs placeholder 0.0
#   catalyst_proximity_hours: default 168.0 (no catalyst) vs placeholder 0.0
#   lifecycle_phase:        default 1.0 vs placeholder 0.0
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

# The 11 fields the rescore write block persists per score
# (scripts/rescore.py `_rescore_in_session`), in the same order as
# tests/test_rescore_compare.py SCORE_WRITE_FIELDS.
SCORE_WRITE_FIELDS = [
    "risk",
    "exit_risk",
    "hype",
    "ethos",
    "liquidity_access",
    "manipulation",
    "confidence",
    "uncertainty",
    "catalyst",
    "research_priority",
    "risk_band",
]


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


@pytest.fixture(autouse=True)
def _prime_ensemble_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the ensemble to deterministic weights in BOTH call sites.

    ``score_assets`` blends through ``scoring.engine.ensemble_engine`` and
    ``engine_score`` through ``ops.score_drift.ensemble_engine``; both names
    must point at the same primed instance or the comparison is meaningless.
    Mirrors tests/test_rescore_compare.py `_prime_ensemble`.
    """

    from scoring.ensemble import EnsembleEngine

    engine = EnsembleEngine()
    engine._current_weights = {"rule": 0.5, "ml": 0.3, "heuristic": 0.2}
    engine._persisted = True
    engine._last_recalibrate = time.monotonic()
    monkeypatch.setattr(engine_mod, "ensemble_engine", engine)
    monkeypatch.setattr("ops.score_drift.ensemble_engine", engine)


@pytest.fixture()
def scored_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Session, dict[int, dict[str, FeatureValue]]]:
    """Run the REAL score_assets over two controlled tokens on an in-memory DB.

    Token 1 (SPREADMISS) has the three differing-default features missing
    (mf=1, placeholder 0.0); token 2 (SPREADOK) has them present. Everything
    else is patched to deterministic no-ops except the real compute_scores,
    the real _upsert_score/_upsert_explanation, and the real ensemble blend.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    chain = models.Chain(slug="solana", name="Solana", vm_type="solana", native_symbol="SOL")
    session.add(chain)
    session.flush()
    for asset_id, symbol in ((1, "SPREADMISS"), (2, "SPREADOK")):
        session.add(
            models.Asset(
                id=asset_id,
                chain_id=chain.id,
                symbol=symbol,
                address=f"addr_{symbol.lower()}",
                first_seen_at=DECISION_TS,
            )
        )
    session.flush()

    feature_maps = {
        1: _feature_map(HEALTHY_FEATURES, MISSING_FEATURES, extra_missing=True),
        2: _feature_map(HEALTHY_FEATURES, PRESENT_FEATURES, extra_missing=False),
    }

    monkeypatch.setattr(
        engine_mod,
        "get_settings",
        lambda: SimpleNamespace(llm_enabled=False, rpc_pool_enabled=False, model_version="test"),
    )
    monkeypatch.setattr(
        engine_mod,
        "build_and_persist_features",
        lambda session, **kwargs: feature_maps,
    )
    monkeypatch.setattr(engine_mod, "mask_unreliable_forecast", lambda session, raw: (raw, False))
    monkeypatch.setattr(
        "scoring.cross_source_fusion.fuse_signals",
        lambda session, asset_id: SimpleNamespace(confidence_boost=0.0, source_count=0),
    )
    monkeypatch.setattr("scoring.cross_source_fusion.persist_fusion", lambda session, fusion: None)
    monkeypatch.setattr(ScoringEngine, "_rpc_pool_uncertainty", lambda self, session, asset_id: 0.0)
    monkeypatch.setattr(ScoringEngine, "_maybe_create_alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(ScoringEngine, "_record_risk_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(ScoringEngine, "_build_price_updates", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine_mod.llm_calibrator, "calibrate", lambda session: None)

    scores = ScoringEngine().score_assets(session, decision_ts=DECISION_TS)
    assert len(scores) == 2

    yield session, feature_maps
    session.close()


def _mf0_only(feature_map: dict[str, FeatureValue]) -> tuple[dict[str, float], list[str]]:
    """The rescore convention: non-missing values only, plus the missing list."""
    features = {name: feature.value for name, feature in feature_map.items() if not feature.missing}
    missing = [name for name, feature in feature_map.items() if feature.missing]
    return features, missing


def _all_values(feature_map: dict[str, FeatureValue]) -> tuple[dict[str, float], list[str]]:
    """The OLD engine convention: every stored value, including mf=1 0.0s."""
    features = {name: feature.value for name, feature in feature_map.items()}
    missing = [name for name, feature in feature_map.items() if feature.missing]
    return features, missing


class TestScoringPathConsistency:
    """score_assets must persist exactly what the rescore would write."""

    def test_engine_matches_rescore_on_missing_features(
        self, scored_session: tuple[Session, dict[int, dict[str, FeatureValue]]]
    ) -> None:
        """Persisted Score rows equal engine_score (the rescore path) field for field.

        Covers all 11 SCORE_WRITE_FIELDS, for the token with missing features
        (the regression) and the control token with everything present.
        """
        session, feature_maps = scored_session
        for asset_id, feature_map in feature_maps.items():
            features, missing = _mf0_only(feature_map)
            expected = engine_score(session, features, missing, masked=False)
            saved = session.scalars(
                select(models.Score).where(models.Score.asset_id == asset_id)
            ).one()
            for name in ("risk", "hype", "confidence", "research_priority"):
                assert getattr(saved, name) == pytest.approx(getattr(expected, name)), (
                    f"asset {asset_id} field {name}: persisted {getattr(saved, name)} "
                    f"!= rescore {getattr(expected, name)}"
                )
            assert saved.risk_band == expected.risk_band
            # The engine records the weights its blend used (migration
            # 0042); the rescore path reports the same ones.
            assert saved.ensemble_weights == expected.weights
            for name in (
                "exit_risk",
                "ethos",
                "liquidity_access",
                "manipulation",
                "uncertainty",
                "catalyst",
            ):
                assert getattr(saved, name) == pytest.approx(getattr(expected.rule_result, name)), (
                    f"asset {asset_id} field {name}: persisted {getattr(saved, name)} "
                    f"!= rescore {getattr(expected.rule_result, name)}"
                )

    def test_old_convention_differs_by_exact_fingerprint(
        self, scored_session: tuple[Session, dict[int, dict[str, FeatureValue]]]
    ) -> None:
        """The pre-fix convention would have written provably different values.

        On the token with missing features the old raw dict (mf=1 0.0s
        included) differs from the fixed values by exact amounts, in the
        directions the formulas dictate: the fix RAISES exit_risk by
        15/1.90 = 7.8947 (the old code scored a missing spread as perfect
        instead of worst-case, understating exit risk), and LOWERS catalyst
        by exactly 12.0 (a missing proximity scored as "catalyst right
        now") and liquidity_access by exactly 25.0 (a missing spread scored
        as perfect spread quality). On the control token the old convention
        is identical -- the fix is a no-op when nothing is missing.
        """
        session, feature_maps = scored_session
        for asset_id, feature_map in feature_maps.items():
            features_old, missing = _all_values(feature_map)
            old = compute_scores(features_old, missing, data_layer_uncertainty=0.0, session=session)
            saved = session.scalars(
                select(models.Score).where(models.Score.asset_id == asset_id)
            ).one()
            if asset_id == 1:  # SPREADMISS: the regression fingerprint
                # exit_risk: the fix raises it (missing spread is worst-case,
                # not perfect) -- old understated it by 7.8947.
                assert saved.exit_risk - old.exit_risk == pytest.approx(7.8947, abs=1e-3)
                # catalyst / liquidity_access: the fix lowers them (missing
                # proximity is not "catalyst now"; missing spread is not
                # perfect spread quality) -- old overstated both.
                assert old.catalyst - saved.catalyst == pytest.approx(12.0, abs=1e-3)
                assert old.liquidity_access - saved.liquidity_access == pytest.approx(
                    25.0, abs=1e-3
                )
            else:  # SPREADOK: control -- old and new conventions agree
                assert old.exit_risk == pytest.approx(saved.exit_risk)
                assert old.catalyst == pytest.approx(saved.catalyst)
                assert old.liquidity_access == pytest.approx(saved.liquidity_access)

    def test_missing_features_still_recorded(
        self, scored_session: tuple[Session, dict[int, dict[str, FeatureValue]]]
    ) -> None:
        """The missing list still flows through to the persisted explanation."""
        session, _ = scored_session
        saved = session.scalars(select(models.Score).where(models.Score.asset_id == 1)).one()
        explanation = session.scalars(
            select(models.ScoreExplanation).where(models.ScoreExplanation.score_id == saved.id)
        ).one()
        # compute_scores unions the passed-in missing list with the rules
        # engine's own missing features (every expected-but-absent name), so
        # the persisted list is a superset of the three mf=1 names.
        assert set(MISSING_FEATURES) <= set(explanation.missing_features)
