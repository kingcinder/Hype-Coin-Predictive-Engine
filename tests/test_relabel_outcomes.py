from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from scripts.relabel_outcomes_v2 import relabel
from storage import models
from storage.repository import insert_market_snapshot_once
from tests.conftest import seed_market_asset

SCORED_AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _seed_v1(session, asset) -> models.RiskOutcome:
    score = models.Score(
        asset_id=asset.id,
        decision_ts=SCORED_AT,
        observed_at=SCORED_AT,
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
        model_version="test-relabel",
        confidence_source="fallback",
    )
    session.add(score)
    session.flush()
    outcome = models.RiskOutcome(
        asset_id=asset.id,
        score_id=score.id,
        risk_band="BLACK",
        scored_at=SCORED_AT,
        lifecycle_phase_at_score="seeding",
        evaluated_at=SCORED_AT,
        # v1 unbounded label: the collapse happened long after the window.
        collapsed=True,
        survived=False,
        label_version="v1_unbounded",
    )
    session.add(outcome)
    session.flush()
    return outcome


def _seed_lifecycle(session, asset, phase: str, ts: datetime) -> None:
    session.add(
        models.LifecycleEvent(
            asset_id=asset.id,
            phase=phase,
            event_type="phase_transition",
            ts=ts,
            observed_at=ts,
            confidence=0.9,
            details={},
        )
    )
    session.flush()


def test_relabel_appends_v2_copy_and_keeps_v1(session) -> None:
    asset = seed_market_asset(session)
    old = _seed_v1(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=1))

    counts = relabel(session, apply=True, now=SCORED_AT + timedelta(days=5))
    assert counts == {"candidates": 1, "copied": 1, "censored": 0}
    session.flush()

    rows = session.scalars(
        select(models.RiskOutcome).where(models.RiskOutcome.score_id == old.score_id)
    ).all()
    assert len(rows) == 2
    v1 = next(row for row in rows if row.label_version == "v1_unbounded")
    v2 = next(row for row in rows if row.label_version == "v2_bounded")
    assert v1.id == old.id
    assert v1.correction_of is None
    assert v2.correction_of == old.id
    assert v2.collapsed is True
    assert v2.survived is False
    # SQLite round-trips timestamps without tzinfo.
    assert v2.evaluated_at.replace(tzinfo=UTC) == SCORED_AT + timedelta(days=5)


def test_relabel_censors_collapse_outside_window(session) -> None:
    asset = seed_market_asset(session)
    _seed_v1(session, asset)
    # Collapse only AFTER the observation window: v1 counted it, bounded does not.
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(days=7))

    counts = relabel(session, apply=True, now=SCORED_AT + timedelta(days=10))
    assert counts == {"candidates": 1, "copied": 0, "censored": 1}
    rows = session.scalars(select(models.RiskOutcome)).all()
    assert len(rows) == 1
    assert rows[0].label_version == "v1_unbounded"


def test_relabel_skips_window_not_elapsed(session) -> None:
    asset = seed_market_asset(session)
    _seed_v1(session, asset)
    _seed_lifecycle(session, asset, "survivor", SCORED_AT + timedelta(hours=1))

    counts = relabel(session, apply=True, now=SCORED_AT + timedelta(hours=2))
    assert counts == {"candidates": 1, "copied": 0, "censored": 1}


def test_relabel_censors_survival_without_forward_price(session) -> None:
    """A v1 survival label with no forward price data is not established:
    no v2 copy is made, the row stays v1 and counts censored."""
    asset = seed_market_asset(session)
    _seed_v1(session, asset)
    _seed_lifecycle(session, asset, "survivor", SCORED_AT + timedelta(hours=1))

    counts = relabel(session, apply=True, now=SCORED_AT + timedelta(days=5))
    assert counts == {"candidates": 1, "copied": 0, "censored": 1}
    rows = session.scalars(select(models.RiskOutcome)).all()
    assert len(rows) == 1
    assert rows[0].label_version == "v1_unbounded"


def test_relabel_copies_survival_with_forward_price(session) -> None:
    """The same v1 survival label WITH a forward snapshot copies to v2."""
    asset = seed_market_asset(session)
    old = _seed_v1(session, asset)
    _seed_lifecycle(session, asset, "survivor", SCORED_AT + timedelta(hours=1))
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert pair is not None and source is not None
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=SCORED_AT + timedelta(hours=24),
        observed_at=SCORED_AT + timedelta(hours=24),
        price_usd=1.10,
        volume_usd=50_000,
        buys=30,
        sells=5,
        trades=35,
    )
    session.flush()

    counts = relabel(session, apply=True, now=SCORED_AT + timedelta(days=5))
    assert counts == {"candidates": 1, "copied": 1, "censored": 0}
    rows = session.scalars(
        select(models.RiskOutcome).where(models.RiskOutcome.score_id == old.score_id)
    ).all()
    assert len(rows) == 2
    v2 = next(row for row in rows if row.label_version == "v2_bounded")
    assert v2.survived is True
    assert v2.collapsed is False


def test_relabel_is_idempotent(session) -> None:
    asset = seed_market_asset(session)
    _seed_v1(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=1))

    now = SCORED_AT + timedelta(days=5)
    relabel(session, apply=True, now=now)
    session.flush()
    second = relabel(session, apply=True, now=now)
    assert second["copied"] == 0
    rows = session.scalars(
        select(models.RiskOutcome).where(models.RiskOutcome.label_version == "v2_bounded")
    ).all()
    assert len(rows) == 1
