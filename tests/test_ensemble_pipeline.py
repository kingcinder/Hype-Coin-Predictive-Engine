"""End-to-end ensemble pipeline integration test (punchlist Phase 2, item 13).

Proves the whole adaptive-learning loop holds together:

1. ``score_current_assets`` records a ``RiskOutcome`` whose ``details`` carry
   the rule band, the ML band (from ``collapse_probability_24h``) AND the
   heuristic band (from ``ignition_signal``) as SEPARATE predictions —
   ``RiskOutcome.details["ml_risk_band"]`` and
   ``RiskOutcome.details["heuristic_band"]`` (items 9-10).
2. ``evaluate_outcomes`` feeds the observed outcome to ALL THREE scorers in a
   single ``record_outcomes`` batch call, confidence-weighted (item 11).
3. The ensemble weights adapt, and a forced ``persist()`` writes an
   ``EnsembleState`` row with ``weight_history`` (item 12).

Fixture note: ``build_and_persist_features`` RECOMPUTES every feature from
its source tables, so seeding ``Feature`` rows directly would be wiped.  The
ML probability is sourced from the ``Forecast`` table and the ignition
signal from ``IgnitionEvent`` rows, so those are the rows this test seeds.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from common.enums import IgnitionEventType
from scoring.engine import score_current_assets
from scoring.ensemble import EnsembleEngine
from storage import models
from tests.conftest import seed_market_asset

DECISION = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _patch_db_session(monkeypatch, session) -> None:
    """Point storage.database.session_scope at fresh sessions on the fixture
    engine so the ensemble singleton's saves land in the test DB."""
    from storage import database

    maker = sessionmaker(
        bind=session.get_bind(), autoflush=False, autocommit=False, expire_on_commit=False
    )

    @contextmanager
    def _test_session():
        fresh = maker()
        try:
            yield fresh
        finally:
            fresh.close()

    monkeypatch.setattr(database, "session_scope", _test_session)


def _read_state_fresh(session) -> models.EnsembleState | None:
    """Read EnsembleState through a brand-new session to prove durability."""
    maker = sessionmaker(
        bind=session.get_bind(), autoflush=False, autocommit=False, expire_on_commit=False
    )
    with maker() as verify:
        return verify.scalar(select(models.EnsembleState).limit(1))


def _seed_band_features(session, asset: models.Asset) -> None:
    """Give the asset an ML collapse probability and an ignition event.

    These are the SOURCE rows the feature factory reads when it recomputes
    features at scoring time — a ``Forecast`` row with a high collapse
    probability feeds ``collapse_probability_24h``, and a fresh
    ``IgnitionEvent`` feeds ``ignition_signal`` — so the ML and heuristic
    bands are populated SEPARATELY in the outcome's details.
    """
    session.add(
        models.Forecast(
            asset_id=asset.id,
            decision_ts=DECISION,
            observed_at=DECISION,
            p_ignition_24h=0.5,
            p_collapse_24h=0.85,
            model_version="test-ensemble-pipeline",
            calibrated=False,
            details={},
        )
    )
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert source is not None  # created by seed_reference inside seed_market_asset
    session.add(
        models.IgnitionEvent(
            asset_id=asset.id,
            source_id=source.id,
            event_type=IgnitionEventType.FIRST_LIQUIDITY_INJECTION.value,
            ts=DECISION - timedelta(hours=1),
            observed_at=DECISION - timedelta(hours=1),
            confidence=0.9,
            details={},
        )
    )
    session.flush()


def test_ensemble_pipeline_stores_separate_bands_and_learns(session, monkeypatch) -> None:
    """Full loop: score → separate bands in details → evaluate → all three
    scorers get feedback → weights persist with history."""
    import scoring.ensemble as ensemble_mod
    from risk_engine.outcomes import evaluate_outcomes

    # Fresh engine (skip DB load), patched into the module that
    # evaluate_outcomes imports at call time, and patched session_scope so
    # saves land in the test DB.
    engine = EnsembleEngine()
    engine._persisted = True
    engine._pending_outcomes = 0
    monkeypatch.setattr(ensemble_mod, "ensemble_engine", engine)
    _patch_db_session(monkeypatch, session)

    # Low-liquidity asset -> BLACK rule band (non-GREEN so an outcome is
    # recorded), plus ML + heuristic band source rows.
    asset = seed_market_asset(session, low_liquidity=True)
    _seed_band_features(session, asset)
    # Note: scoring.engine binds the ORIGINAL ensemble singleton at import
    # time, so its first blend() call may write a benign EnsembleState row
    # (default weights, zero accuracy) via the patched session_scope before
    # this test's engine.persist() below updates that same row.
    scores = score_current_assets(session, decision_ts=DECISION, asset_ids=[asset.id])
    session.commit()
    assert scores

    outcome = session.scalar(
        select(models.RiskOutcome).where(models.RiskOutcome.score_id == scores[0].id)
    )
    assert outcome is not None
    details = outcome.details
    # Item 10: ML and heuristic predictions stored SEPARATELY in details.
    assert details.get("ml_prediction") is True
    assert details.get("ml_risk_band") in {"RED", "ORANGE", "BLACK"}
    assert details.get("heuristic_prediction") is True
    assert details.get("heuristic_band") == "GREEN"
    assert outcome.risk_band == "BLACK"  # rule band is its own column

    # Token collapses after the observation window.
    eval_ts = DECISION + timedelta(hours=49)
    session.add(
        models.LifecycleEvent(
            asset_id=asset.id,
            phase="collapse",
            event_type="phase_transition",
            ts=eval_ts,
            observed_at=eval_ts,
            confidence=0.9,
            details={},
        )
    )
    session.commit()

    # The score's confidence is the weight the ensemble applies to each
    # outcome (clamp(confidence/100, 0.1, 1.0)) — item 11 semantics.
    score_conf = float(scores[0].confidence)
    expected_weight = max(0.1, min(1.0, score_conf / 100.0))

    evaluate_outcomes(session, decision_ts=eval_ts, window_hours=48)
    session.commit()

    # Item 11 + item 9: ALL three scorers received the outcome in one batch,
    # each weighted by the score confidence (fractional totals, not 1).
    accuracy = engine._accuracy
    assert accuracy["rule"].total_predictions == pytest.approx(expected_weight)
    assert accuracy["ml"].total_predictions == pytest.approx(expected_weight)
    assert accuracy["heuristic"].total_predictions == pytest.approx(expected_weight)
    # Rule + ML predicted the negative case (BLACK) -> correct.
    assert accuracy["rule"].correct_predictions == pytest.approx(expected_weight)
    assert accuracy["ml"].correct_predictions == pytest.approx(expected_weight)
    # Heuristic predicted GREEN (positive) but the token collapsed -> wrong.
    assert accuracy["heuristic"].correct_predictions == 0

    # Item 12: weights persist with history on an explicit save.
    engine.persist()
    state = _read_state_fresh(session)
    assert state is not None
    assert state.weight_history, "ensemble weight history must persist"
    assert state.scorer_accuracy["heuristic"]["total_predictions"] == pytest.approx(expected_weight)
    assert state.scorer_accuracy["rule"]["total_predictions"] == pytest.approx(expected_weight)

    # Feedback is one-shot: a second evaluation must not double-feed.
    rule_total_before = engine._accuracy["rule"].total_predictions
    evaluate_outcomes(session, decision_ts=eval_ts, window_hours=48)
    session.commit()
    assert engine._accuracy["rule"].total_predictions == rule_total_before
