"""Rescore all historical tokens using the current scoring formula.

Reads the features table, reconstructs feature dicts per (asset, decision_ts),
re-runs compute_scores() with the live code (including the proportional risk
fix), and updates the scores table in-place.  This is a one-time migration
tool — not part of the regular scoring pipeline.

Usage:
    python scripts/rescore.py [--dry-run] [--compare] [--min-change THRESHOLD]

Options:
    --dry-run          Compute scores but don't write to the database.
    --compare          Print old → new risk for each token (implies --dry-run).
    --min-change       Minimum |old - new| to include in --compare output
                       (default: 0, meaning all tokens).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging import get_logger
from scoring.formulas import compute_scores
from storage import models
from storage.database import session_scope

log = get_logger(__name__)


def _build_features(session: Session) -> dict[tuple[int, datetime], dict[str, float]]:
    """Reconstruct feature dicts from the features table.

    Returns {(asset_id, decision_ts): {feature_name: feature_value}}.
    Missing features (missing_flag=True) are excluded from the dict so
    compute_scores treats them as truly absent.
    """
    features_map: dict[tuple[int, datetime], dict[str, float]] = defaultdict(dict)
    rows = session.execute(
        select(
            models.Feature.asset_id,
            models.Feature.decision_ts,
            models.Feature.feature_name,
            models.Feature.feature_value,
            models.Feature.missing_flag,
        )
        .where(models.Feature.missing_flag.is_(False))
        .order_by(models.Feature.asset_id, models.Feature.decision_ts)
    ).all()
    for asset_id, decision_ts, name, value, _ in rows:
        key = (
            asset_id,
            decision_ts.replace(tzinfo=UTC) if decision_ts.tzinfo is None else decision_ts,
        )
        features_map[key][name] = float(value)
    log.info("rescore_features_loaded", pairs=len(features_map))
    return dict(features_map)


def _build_missing(session: Session) -> dict[tuple[int, datetime], list[str]]:
    """Collect missing feature names per (asset, decision_ts)."""
    missing_map: dict[tuple[int, datetime], list[str]] = defaultdict(list)
    rows = session.execute(
        select(
            models.Feature.asset_id,
            models.Feature.decision_ts,
            models.Feature.feature_name,
        ).where(models.Feature.missing_flag.is_(True))
    ).all()
    for asset_id, decision_ts, name in rows:
        key = (
            asset_id,
            decision_ts.replace(tzinfo=UTC) if decision_ts.tzinfo is None else decision_ts,
        )
        missing_map[key].append(name)
    return dict(missing_map)


def rescore(
    dry_run: bool = False,
    compare: bool = False,
    min_change: float = 0.0,
    session: Session | None = None,
) -> dict[str, int]:
    """Rescore all scored tokens and return stats.

    Args:
        dry_run:  Compute but don't write to the database.
        compare:  Print old → new risk for each token (implies dry_run).
        min_change: Minimum |old_risk - new_risk| to include in output.
        session:  Optional session to run against (used by tests that need
            a controlled in-memory database); defaults to the configured DB.
    """

    def _run(active: Session) -> dict[str, int]:
        return _rescore_in_session(active, dry_run=dry_run, compare=compare, min_change=min_change)

    if session is not None:
        return _run(session)
    with session_scope() as active_session:
        return _run(active_session)


def _rescore_in_session(
    session: Session,
    *,
    dry_run: bool,
    compare: bool,
    min_change: float,
) -> dict[str, int]:
    """The rescore body, running against a caller-provided session.

    Does not close the session — the caller owns its lifecycle (``rescore``
    wraps a ``session_scope`` that handles cleanup; tests inject an in-memory
    session they keep open). Commits only when ``dry_run`` is False: that is
    the rescore contract, and it applies to injected sessions too.
    """
    features_map = _build_features(session)
    missing_map = _build_missing(session)

    # Load existing scores
    scores = session.scalars(select(models.Score).order_by(models.Score.id)).all()
    log.info("rescore_scores_loaded", count=len(scores))

    # Pre-fetch asset symbols to avoid N+1 lookups during compare
    asset_map: dict[int, str] = {}
    if compare:
        dry_run = True
        for asset in session.scalars(select(models.Asset)).all():
            asset_map[asset.id] = asset.symbol or f"id={asset.id}"

    updated = 0
    skipped = 0
    errors = 0
    risk_after: list[float] = []
    compare_rows: list[tuple[str, float, float, float]] = []  # (symbol, old, new, diff)

    for score in scores:
        ts = score.decision_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        key = (score.asset_id, ts)
        feat = features_map.get(key)
        if not feat:
            skipped += 1
            continue

        missing = missing_map.get(key, [])
        try:
            result = compute_scores(
                feat,
                missing,
                session=session,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("rescore_error", score_id=score.id, error=str(exc))
            errors += 1
            continue

        risk_after.append(result.risk)

        if compare and abs(result.risk - score.risk) >= min_change:
            sym = asset_map.get(score.asset_id, f"id={score.asset_id}")
            compare_rows.append((sym, score.risk, result.risk, result.risk - score.risk))

        if not dry_run:
            score.risk = result.risk
            score.exit_risk = result.exit_risk
            score.hype = result.hype
            score.ethos = result.ethos
            score.liquidity_access = result.liquidity_access
            score.manipulation = result.manipulation
            score.confidence = result.confidence
            score.uncertainty = result.uncertainty
            score.catalyst = result.catalyst
            score.research_priority = result.research_priority
            score.risk_band = result.risk_band.value
        updated += 1

    if not dry_run:
        session.commit()
        log.info("rescore_committed", updated=updated)

    # Print per-token diffs if --compare (header always prints so an empty
    # diff is visible instead of looking like the flag was ignored).
    if compare:
        compare_rows.sort(key=lambda r: abs(r[3]), reverse=True)
        print(f"\n--- Risk changes ({len(compare_rows)} tokens, min_change={min_change}) ---")
        print(f"{'Symbol':>20s}  {'Old':>8s}  {'New':>8s}  {'Delta':>8s}")
        print("-" * 52)
        for sym, old, new, diff in compare_rows[:50]:  # top 50 by |delta|
            print(f"{sym:>20s}  {old:>8.2f}  {new:>8.2f}  {diff:>+8.2f}")
        if len(compare_rows) > 50:
            print(f"  ... and {len(compare_rows) - 50} more (showing top 50 by |delta|)")
        print()

    # Print before/after distribution
    if risk_after:
        ra = np.array(risk_after)
        unique = len(np.unique(np.round(ra, 2)))
        print(f"\n{'=' * 60}")
        print(f"Rescore complete ({'DRY RUN' if dry_run else 'APPLIED'})")
        print(f"  Updated: {updated}, Skipped: {skipped}, Errors: {errors}")
        print(f"  Risk scores: {unique} distinct values (was 2-4)")
        print(f"  Risk range: {ra.min():.2f} – {ra.max():.2f}")
        print(f"  Risk mean: {ra.mean():.2f}, std: {ra.std():.2f}")
        # Top 10 distinct values
        from collections import Counter

        rounded = np.round(ra, 2)
        counts = Counter(rounded)
        print("  Top 10 distinct values:")
        for val, cnt in counts.most_common(10):
            print(f"    {val:>8.2f}: {cnt} tokens")
        print(f"{'=' * 60}")

    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "distinct_risk_values": len(np.unique(np.round(ra, 2))) if risk_after else 0,
        "compare_rows": len(compare_rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rescore all historical tokens")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't write")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Print old → new risk for each token (implies --dry-run)",
    )
    parser.add_argument(
        "--min-change",
        type=float,
        default=0.0,
        help="Minimum |old - new| to include in --compare output (default: 0)",
    )
    args = parser.parse_args()
    rescore(dry_run=args.dry_run, compare=args.compare, min_change=args.min_change)
