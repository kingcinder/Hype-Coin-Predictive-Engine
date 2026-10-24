from __future__ import annotations

from contextlib import nullcontext

from sqlalchemy import func, select

from storage import models, seed


def test_reference_seed_is_idempotent(session, monkeypatch) -> None:
    monkeypatch.setattr(seed, "SessionLocal", lambda: nullcontext(session))

    seed.seed_reference_data()
    first = (
        session.scalar(select(func.count()).select_from(models.Chain)),
        session.scalar(select(func.count()).select_from(models.Source)),
        session.scalar(select(func.count()).select_from(models.Venue)),
    )
    first_chain_ids = {row.slug: row.id for row in session.scalars(select(models.Chain)).all()}
    first_venue_ids = {row.name: row.id for row in session.scalars(select(models.Venue)).all()}

    seed.seed_reference_data()

    second = (
        session.scalar(select(func.count()).select_from(models.Chain)),
        session.scalar(select(func.count()).select_from(models.Source)),
        session.scalar(select(func.count()).select_from(models.Venue)),
    )
    assert first == second == (3, 8, 3)
    current_chain_ids = {row.slug: row.id for row in session.scalars(select(models.Chain)).all()}
    current_venue_ids = {row.name: row.id for row in session.scalars(select(models.Venue)).all()}
    assert current_chain_ids == first_chain_ids
    assert current_venue_ids == first_venue_ids
