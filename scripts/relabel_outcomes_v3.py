"""Relabel ``risk_outcomes`` rows with the bounded label version ``v3_bounded``.

Append-only (L8): every bounded label is a NEW row with ``correction_of`` set
to the row it supersedes. Source rows are never modified or deleted.

v3 excludes DEAD from the collapse phases (staleness is not a price crash)
and gates outcome recording on recent market data, so v2 rows labeled
collapsed only because their phase went DEAD are re-labeled as censored
(or re-evaluated under the corrected rules) instead of collapsed.

Usage:
    python -m scripts.relabel_outcomes_v3            # dry-run (prints counts)
    python -m scripts.relabel_outcomes_v3 --apply    # write v3 copies
    python -m scripts.relabel_outcomes_v3 --apply --batch 500
    python -m scripts.relabel_outcomes_v3 --apply --start 2026-11-20

Only evaluated ``v1_unbounded`` / ``v2_bounded`` rows that do not already
have a v3 copy are considered. Rows whose observation window has not
elapsed, or whose lifecycle phase inside ``[scored_at, scored_at + window]``
neither collapses nor survives, stay at their old version and are reported
as censored (L5: unknown stays unknown).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.time import ensure_utc, utc_now
from risk_engine.outcomes import (
    _COLLAPSE_PHASES,
    _SURVIVAL_PHASES,
    LABEL_VERSION,
    _has_forward_price_evidence,
    _lifecycle_phase_as_of,
    _price_change_since,
)
from storage import models
from storage.database import session_scope
from storage.repository import queued_write
from storage.write_queue import start_write_queue, stop_write_queue


def _candidate_rows(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[models.RiskOutcome]:
    """Evaluated v1/v2 rows that do not already have a v3 copy."""
    done = select(models.RiskOutcome.score_id).where(
        models.RiskOutcome.label_version == LABEL_VERSION
    )
    conditions = [
        models.RiskOutcome.label_version.in_(["v1_unbounded", "v2_bounded"]),
        models.RiskOutcome.evaluated_at.is_not(None),
        models.RiskOutcome.score_id.not_in(done),
    ]
    if start is not None:
        conditions.append(models.RiskOutcome.scored_at >= ensure_utc(start))
    if end is not None:
        conditions.append(models.RiskOutcome.scored_at < ensure_utc(end))
    return list(
        session.scalars(
            select(models.RiskOutcome)
            .where(*conditions)
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
        if phase in _SURVIVAL_PHASES and not _has_forward_price_evidence(
            session, old.asset_id, score_time, window_end, as_of=now
        ):
            # Survival without forward price data is not established (see
            # risk_engine.outcomes._has_forward_price_evidence): the row
            # stays at its old version, counted censored, never copied as survived.
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
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, int]:
    """Copy eligible v1/v2 rows to bounded v3 rows; return counts."""
    now = ensure_utc(now or utc_now())
    rows = _candidate_rows(session, start=start, end=end)
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
    parser.add_argument(
        "--start",
        default=None,
        help="Only re-label rows with scored_at >= this ISO date/datetime.",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Only re-label rows with scored_at < this ISO date/datetime.",
    )
    args = parser.parse_args()
    if args.batch < 1:
        parser.error("--batch must be >= 1")

    now = utc_now()
    start = (
        datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        if args.start
        else None
    )
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
        if args.end
        else None
    )
    if args.apply:
        start_write_queue()
        try:
            with session_scope() as session:
                ids = [row.id for row in _candidate_rows(session, start=start, end=end)]
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
            counts = relabel(session, apply=False, now=now, start=start, end=end)
        print(f"would_create={counts['copied']} censored={counts['censored']}")
        print("Run with --apply to write the v3 copies.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
