"""Outcome labels must be bounded to their prediction's observation window.

Regression coverage for the red-team finding that ``evaluate_outcomes`` used an
unbounded "latest lifecycle event" lookup: a token that collapsed after its
window ended was labelled collapsed, leaking post-window knowledge into band
calibration, the validation metrics, and the ensemble feedback loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from common.time import ensure_utc
from risk_engine.outcomes import (
    UNRESOLVED_OUTCOME_COMPONENT,
    evaluate_outcomes,
    record_risk_outcome,
)
from storage import models
from storage.repository import insert_market_snapshot_once
from tests.conftest import seed_market_asset

SCORED_AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
WINDOW_HOURS = 48.0
WINDOW_END = SCORED_AT + timedelta(hours=WINDOW_HOURS)
EVAL_TS = SCORED_AT + timedelta(days=5)


def _seed_score(
    session,
    asset: models.Asset,
    *,
    decision_ts: datetime = SCORED_AT,
    model_version: str = "test-outcomes",
) -> models.Score:
    score = models.Score(
        asset_id=asset.id,
        decision_ts=decision_ts,
        observed_at=decision_ts,
        hype=0.5,
        ethos=0.5,
        risk=90.0,
        liquidity_access=0.5,
        manipulation=0.1,
        confidence=80.0,
        uncertainty=0.2,
        catalyst=0.3,
        exit_risk=0.9,
        research_priority=0.5,
        risk_band="BLACK",
        model_version=model_version,
    )
    session.add(score)
    session.flush()
    return score


def _seed_lifecycle(
    session, asset: models.Asset, phase: str, ts: datetime
) -> models.LifecycleEvent:
    event = models.LifecycleEvent(
        asset_id=asset.id,
        phase=phase,
        event_type="phase_transition",
        ts=ts,
        observed_at=ts,
        confidence=0.9,
        details={},
    )
    session.add(event)
    session.flush()
    return event


def _seed_prediction(
    session,
    asset: models.Asset,
    *,
    decision_ts: datetime = SCORED_AT,
    model_version: str = "test-outcomes",
) -> models.RiskOutcome:
    score = _seed_score(session, asset, decision_ts=decision_ts, model_version=model_version)
    record_risk_outcome(
        session,
        asset_id=asset.id,
        risk_band="BLACK",
        score_id=score.id,
        decision_ts=decision_ts,
    )
    outcome = session.scalar(
        select(models.RiskOutcome).where(models.RiskOutcome.score_id == score.id)
    )
    assert outcome is not None
    return outcome


def test_post_window_collapse_does_not_label_prediction(session) -> None:
    """Score Day 0, window ends Day 2, collapse Day 4 -> unresolved, not collapsed."""
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(days=4))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.collapsed is False
    assert outcome.survived is False
    assert outcome.evaluated_at is None
    assert outcome.lifecycle_phase_at_eval == "unknown"


def test_collapse_inside_window_labels_collapsed(session) -> None:
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.collapsed is True
    assert outcome.rugged is False
    assert outcome.survived is False
    assert ensure_utc(outcome.evaluated_at) == EVAL_TS
    assert outcome.lifecycle_phase_at_eval == "collapse"


def test_rug_inside_window_labels_rugged_and_collapsed(session) -> None:
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "rugged", SCORED_AT + timedelta(hours=12))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.rugged is True
    assert outcome.collapsed is True
    assert outcome.survived is False
    assert ensure_utc(outcome.evaluated_at) == EVAL_TS


def test_survival_phase_at_window_end_labels_survived(session) -> None:
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "survivor", SCORED_AT + timedelta(hours=30))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.survived is True
    assert outcome.collapsed is False
    assert outcome.rugged is False
    assert ensure_utc(outcome.evaluated_at) == EVAL_TS
    assert outcome.lifecycle_phase_at_eval == "survivor"


def test_no_lifecycle_events_in_window_stays_unresolved(session) -> None:
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.collapsed is False
    assert outcome.survived is False
    assert outcome.evaluated_at is None
    assert outcome.lifecycle_phase_at_eval == "unknown"


def test_price_change_ignores_snapshots_after_window_end(session) -> None:
    """The last in-window snapshot is the exit price; later ones are ignored."""
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "survivor", SCORED_AT + timedelta(hours=40))
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert pair is not None
    assert source is not None

    # seed_market_asset's last entry snapshot is 1.10 at SCORED_AT.
    for hours, price in ((24, 2.20), (72, 11.0)):
        ts = SCORED_AT + timedelta(hours=hours)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=50_000,
            buys=30,
            sells=5,
            trades=35,
        )
    session.flush()

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    # (2.20 - 1.10) / 1.10; the 11.0 post-window snapshot must not be used.
    assert outcome.price_change_pct == pytest.approx(1.0)


def test_report_counts_unresolved_rows_past_their_window(session) -> None:
    asset = seed_market_asset(session)
    withheld = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(days=4))
    # A later prediction whose 48h window has not elapsed yet is not unresolved.
    _seed_prediction(session, asset, decision_ts=SCORED_AT + timedelta(days=4))

    resolved_asset = seed_market_asset(
        session,
        address="Resolved111111111111111111111111111111111",
        pair_address="ResolvedPair11111111111111111111111111111",
    )
    _seed_prediction(session, resolved_asset, model_version="test-resolved")
    _seed_lifecycle(session, resolved_asset, "collapse", SCORED_AT + timedelta(hours=24))

    report = evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert withheld.evaluated_at is None
    assert report.unresolved == 1

    # The backlog stays visible on later passes instead of vanishing.
    report_again = evaluate_outcomes(
        session, decision_ts=EVAL_TS + timedelta(days=1), window_hours=WINDOW_HOURS
    )
    assert report_again.unresolved == 1


def test_health_reports_unresolved_backlog_and_flags_growth(session) -> None:
    """The backlog is a health component, and growth turns it yellow."""
    asset = seed_market_asset(session)
    _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(days=4))

    held = seed_market_asset(
        session,
        address="Held111111111111111111111111111111111111",
        pair_address="HeldPair11111111111111111111111111111111",
    )
    _seed_lifecycle(session, held, "collapse", SCORED_AT + timedelta(days=4))

    def _health_rows() -> list[models.SystemHealth]:
        return list(
            session.scalars(
                select(models.SystemHealth)
                .where(models.SystemHealth.component == UNRESOLVED_OUTCOME_COMPONENT)
                .order_by(models.SystemHealth.ts.asc())
            )
        )

    report = evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)
    assert report.unresolved == 1
    rows = _health_rows()
    assert rows[-1].error_count == 1
    # A backlog younger than three windows is yellow.
    assert rows[-1].state == "yellow"

    _seed_prediction(session, held, model_version="test-held")
    report = evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)
    assert report.unresolved == 2
    rows = _health_rows()
    assert rows[-1].error_count == 2
    # Growth alone stays yellow; only age beyond three windows is red.
    assert report.unresolved_grew is True
    assert rows[-1].state == "yellow"
    assert "GREW" in (rows[-1].message or "")

    # Oldest unresolved row is now more than three windows old -> red.
    report = evaluate_outcomes(
        session, decision_ts=SCORED_AT + timedelta(days=7), window_hours=WINDOW_HOURS
    )
    assert report.unresolved == 2
    rows = _health_rows()
    assert rows[-1].state == "red"

    # Backfill lifecycle state inside both windows: the backlog resolves and
    # the component returns to ok.
    for target in (asset, held):
        _seed_lifecycle(session, target, "collapse", SCORED_AT + timedelta(hours=1))
    report = evaluate_outcomes(
        session, decision_ts=SCORED_AT + timedelta(days=7, hours=1), window_hours=WINDOW_HOURS
    )
    assert report.unresolved == 0
    rows = _health_rows()
    assert rows[-1].error_count == 0
    assert rows[-1].state == "ok"


def test_window_end_boundary_is_inclusive_and_one_second_later_is_not(session) -> None:
    inside = seed_market_asset(
        session,
        address="BoundaryInside1111111111111111111111111111",
        pair_address="BoundaryInsidePair111111111111111111111111",
    )
    after = seed_market_asset(
        session,
        address="BoundaryAfter11111111111111111111111111111",
        pair_address="BoundaryAfterPair1111111111111111111111111",
    )
    inside_outcome = _seed_prediction(session, inside, model_version="test-boundary-inside")
    after_outcome = _seed_prediction(session, after, model_version="test-boundary-after")
    _seed_lifecycle(session, inside, "collapse", WINDOW_END)
    _seed_lifecycle(session, after, "collapse", WINDOW_END + timedelta(seconds=1))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert inside_outcome.collapsed is True
    assert ensure_utc(inside_outcome.evaluated_at) == EVAL_TS
    assert after_outcome.collapsed is False
    assert after_outcome.evaluated_at is None
    assert after_outcome.lifecycle_phase_at_eval == "unknown"


def test_legacy_row_with_existing_bounded_row_does_not_break_evaluation(session) -> None:
    """Regression: relabelling a superseded v1 row crashed the whole scan.

    ``evaluate_outcomes`` stamps the current label version when it evaluates a
    row. If the append-only relabel already wrote a bounded row for the same
    score, that UPDATE violates ``uq_risk_outcomes_score_label``; the scan
    rolled back and left a red worker health row. The bounded row must be
    evaluated and the superseded v1 row left untouched.
    """
    asset = seed_market_asset(session)
    bounded_row = _seed_prediction(session, asset, model_version="test-legacy-dup")
    score_id = bounded_row.score_id
    legacy = models.RiskOutcome(
        asset_id=asset.id,
        score_id=score_id,
        risk_band="BLACK",
        scored_at=SCORED_AT,
        lifecycle_phase_at_score="unknown",
        label_version="v1_unbounded",
        details={},
    )
    session.add(legacy)
    session.flush()
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert bounded_row.collapsed is True
    assert ensure_utc(bounded_row.evaluated_at) == EVAL_TS
    assert bounded_row.label_version == "v2_bounded"
    assert legacy.evaluated_at is None
    assert legacy.label_version == "v1_unbounded"
    rows = session.scalars(
        select(models.RiskOutcome).where(models.RiskOutcome.score_id == score_id)
    ).all()
    assert len(rows) == 2


def test_evaluated_outcome_is_labelled_v2_bounded(session) -> None:
    """A freshly evaluated outcome carries the bounded label version."""
    from risk_engine.outcomes import LABEL_VERSION

    assert LABEL_VERSION == "v2_bounded"
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset, model_version="test-label-ver")
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert ensure_utc(outcome.evaluated_at) == EVAL_TS
    assert outcome.label_version == "v2_bounded"
