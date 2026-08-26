"""Rescore all historical tokens using the current scoring formula.

Reads the features table, reconstructs feature dicts per (asset, decision_ts),
re-runs compute_scores() with the live code (including the proportional risk
fix), and updates the scores table in-place.  This is a one-time migration
tool — not part of the regular scoring pipeline.

Usage:
    python scripts/rescore.py [--dry-run]
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


def rescore(dry_run: bool = False) -> dict[str, int]:
    """Rescore all scored tokens and return stats."""
    with session_scope() as session:
        features_map = _build_features(session)
        missing_map = _build_missing(session)

        # Load existing scores
        scores = session.scalars(select(models.Score).order_by(models.Score.id)).all()
        log.info("rescore_scores_loaded", count=len(scores))

        updated = 0
        skipped = 0
        errors = 0
        risk_before: list[float] = []
        risk_after: list[float] = []

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

            risk_before.append(score.risk)
            risk_after.append(result.risk)

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
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rescore all historical tokens")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't write")
    args = parser.parse_args()
    rescore(dry_run=args.dry_run)
