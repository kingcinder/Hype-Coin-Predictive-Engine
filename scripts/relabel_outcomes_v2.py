"""Relabel ``risk_outcomes`` rows with the bounded label version ``v2_bounded``.

Append-only (L8): every bounded label is a NEW row with ``correction_of`` set
to the v1 row it supersedes. v1 rows are never modified or deleted.

Usage:
    python -m scripts.relabel_outcomes_v2            # dry-run (prints counts)
    python -m scripts.relabel_outcomes_v2 --apply    # write v2 copies
    python -m scripts.relabel_outcomes_v2 --apply --batch 500

Only evaluated ``v1_unbounded`` rows are considered. Rows whose observation
window has not elapsed, or whose lifecycle phase inside
``[scored_at, scored_at + window]`` neither collapses nor survives, stay v1 and
are reported as censored (L5: unknown stays unknown).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.time import ensure_utc, utc_now
from risk_engine.outcomes import (
    _COLLAPSE_PHASES,
    _SURVIVAL_PHASES,
    LABEL_VERSION,
    _lifecycle_phase_as_of,
    _price_change_since,
)
from storage import models
from storage.database import session_scope
from storage.repository import queued_write
from storage.write_queue import start_write_queue, stop_write_queue


def _candidate_rows(session: Session) -> list[models.RiskOutcome]:
    """Evaluated v1 rows that do not already have a v2 copy."""
    done = select(models.RiskOutcome.score_id).where(
        models.RiskOutcome.label_version == LABEL_VERSION
    )
    return list(
        session.scalars(
            select(models.RiskOutcome)
            .where(
                models.RiskOutcome.label_version == "v1_unbounded",
                models.RiskOutcome.evaluated_at.is_not(None),
                models.RiskOutcome.score_id.not_in(done),
            )
            .order_by(models.RiskOutcome.scored_at.asc())
        ).all()
    )


def _copy_rows(
    session: Session,
    rows: list[models.RiskOutcome],
    *,
    now: datetime,
) -> dict[str, int]:
    """Append bounded copies for ``rows``; return copied/censored counts."""
    settings = get_settings()
    window = timedelta(hours=settings.risk_outcome_window_hours)
    copied = 0
    censored = 0
    for old in rows:
        score_time = ensure_utc(old.scored_at)
        window_end = score_time + window
        if window_end > now:
            censored += 1
            continue
        phase = _lifecycle_phase_as_of(session, old.asset_id, window_end)
        if phase not in _COLLAPSE_PHASES and phase not in _SURVIVAL_PHASES:
            censored += 1
            continue
        session.add(
            models.RiskOutcome(
                asset_id=old.asset_id,
                score_id=old.score_id,
                risk_band=old.risk_band,
                scored_at=score_time,
                lifecycle_phase_at_score=old.lifecycle_phase_at_score,
                evaluated_at=now,
                lifecycle_phase_at_eval=phase,
                price_change_pct=_price_change_since(
                    session, old.asset_id, score_time, until=window_end
                ),
                collapsed=phase in _COLLAPSE_PHASES,
                rugged=phase == "rugged",
                survived=phase in _SURVIVAL_PHASES,
                details=dict(old.details or {}),
                label_version=LABEL_VERSION,
                correction_of=old.id,
            )
        )
        copied += 1
    return {"copied": copied, "censored": censored}


def relabel(
    session: Session,
    *,
    apply: bool,
    now: datetime | None = None,
    batch: int | None = None,
) -> dict[str, int]:
    """Copy eligible v1 rows to bounded v2 rows; return counts."""
    now = ensure_utc(now or utc_now())
    rows = _candidate_rows(session)
    if batch:
        rows = rows[:batch]

    counts = _copy_rows(session, rows, now=now)
    if apply:
        session.flush()
    else:
        session.rollback()
    return {
        "candidates": len(rows),
        "copied": counts["copied"],
        "censored": counts["censored"],
    }


def _relabel_batch(session: Session, ids: list[int], *, now: datetime) -> dict[str, int]:
    """Copy one explicit batch of v1 primary keys (one write-queue transaction)."""
    rows = list(
        session.scalars(
            select(models.RiskOutcome)
            .where(models.RiskOutcome.id.in_(ids))
            .order_by(models.RiskOutcome.scored_at.asc())
        ).all()
    )
    counts = _copy_rows(session, rows, now=now)
    session.flush()
    return {"copied": counts["copied"], "censored": counts["censored"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write v2 copies. Without this flag the script is a dry-run.",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=500,
        help="Rows per write transaction (one queued_write per batch).",
    )
    args = parser.parse_args()
    if args.batch < 1:
        parser.error("--batch must be >= 1")

    now = utc_now()
    if args.apply:
        start_write_queue()
        try:
            with session_scope() as session:
                ids = [row.id for row in _candidate_rows(session)]
            copied = 0
            censored = 0
            for offset in range(0, len(ids), args.batch):
                chunk = ids[offset : offset + args.batch]

                def _run_batch(
                    write_session: Session, batch_ids: list[int] = chunk
                ) -> dict[str, int]:
                    return _relabel_batch(write_session, batch_ids, now=now)

                counts = queued_write(_run_batch)
                copied += counts["copied"]
                censored += counts["censored"]
        finally:
            stop_write_queue()
        print(f"would_create={copied} censored={censored} mode=apply")
    else:
        with session_scope() as session:
            counts = relabel(session, apply=False, now=now)
        print(f"would_create={counts['copied']} censored={counts['censored']}")
        print("Run with --apply to write the v2 copies.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
