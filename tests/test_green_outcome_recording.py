"""Ticket 1 step 6 (M5): GREEN predictions enter risk_outcomes.

Before this change ScoringEngine._record_risk_outcome returned early for
GREEN, so live GREEN false-negatives — the audited failure class (34.3%
of evaluable live GREENs collapsed within 48h) — never entered the
outcome table, and calibration, band validation, and the harness were
structurally blind to them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from scoring.engine import ScoringEngine
from storage import models
from storage.repository import upsert_asset
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0)


def _seed_score(session, *, band: str) -> tuple[models.Asset, models.Score]:
    chain, _source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"Addr{band}1111111111111111111111111111111111",
        symbol=band[:4],
        name=band,
        first_seen_at=T0,
    )
    score = models.Score(
        asset_id=asset.id,
        decision_ts=T0,
        observed_at=T0,
        hype=10.0,
        ethos=10.0,
        risk=10.0,
        liquidity_access=10.0,
        manipulation=0.0,
        confidence=50.0,
        uncertainty=10.0,
        catalyst=0.0,
        exit_risk=10.0,
        research_priority=1.0,
        risk_band=band,
        model_version="test",
    )
    session.add(score)
    session.flush()
    return asset, score


def test_green_score_records_outcome(session) -> None:
    asset, score = _seed_score(session, band="GREEN")
    session.commit()
    ScoringEngine()._record_risk_outcome(
        session, asset_id=asset.id, score=score, decision_ts=T0 + timedelta(hours=1)
    )
    session.commit()
    rows = session.scalars(
        select(models.RiskOutcome).where(models.RiskOutcome.score_id == score.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].risk_band == "GREEN"


def test_red_score_still_records_outcome(session) -> None:
    asset, score = _seed_score(session, band="RED")
    session.commit()
    ScoringEngine()._record_risk_outcome(
        session, asset_id=asset.id, score=score, decision_ts=T0 + timedelta(hours=1)
    )
    session.commit()
    rows = session.scalars(
        select(models.RiskOutcome).where(models.RiskOutcome.score_id == score.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].risk_band == "RED"
