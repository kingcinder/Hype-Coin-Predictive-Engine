"""Relabel ``risk_outcomes`` rows with the bounded label version ``v2_bounded``.

Append-only (L8): every bounded label is a NEW row with ``correction_of`` set
to the v1 row it supersedes. v1 rows are never modified or deleted.

Usage:
    python -m scripts.relabel_outcomes_v2            # dry-run (prints counts)
    python -m scripts.relabel_outcomes_v2 --apply    # write v2 copies
    python -m scripts.relabel_outcomes_v2 --apply --batch 500

Rows whose observation window has not elapsed, or whose lifecycle phase inside
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
from storage.database import SessionLocal
from storage.repository import queued_write
from storage.write_queue import start_write_queue, stop_write_queue


def relabel(
    session: Session,
    *,
    apply: bool,
    now: datetime | None = None,
    batch: int | None = None,
) -> dict[str, int]:
    """Copy eligible v1 rows to bounded v2 rows; return counts."""
    settings = get_settings()
    window = timedelta(hours=settings.risk_outcome_window_hours)
    now = ensure_utc(now or utc_now())

    done = select(models.RiskOutcome.score_id).where(
        models.RiskOutcome.label_version == LABEL_VERSION
    )
    rows = list(
        session.scalars(
            select(models.RiskOutcome)
            .where(
                models.RiskOutcome.label_version != LABEL_VERSION,
                models.RiskOutcome.score_id.not_in(done),
            )
            .order_by(models.RiskOutcome.scored_at.asc())
        ).all()
    )
    if batch:
        rows = rows[:batch]

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

    if apply:
        session.flush()
    else:
        session.rollback()
    return {"candidates": len(rows), "copied": copied, "censored": censored}


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
        default=None,
        help="Maximum number of v1 rows to process in one invocation.",
    )
    args = parser.parse_args()

    if args.apply:
        start_write_queue()
        try:
            counts = queued_write(lambda session: relabel(session, apply=True, batch=args.batch))
        finally:
            stop_write_queue()
    else:
        with SessionLocal() as session:
            counts = relabel(session, apply=False, batch=args.batch)

    print(
        f"candidates={counts['candidates']} copied={counts['copied']} "
        f"censored={counts['censored']} mode={'apply' if args.apply else 'dry-run'}"
    )
    if not args.apply:
        print("Run with --apply to write the v2 copies.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
