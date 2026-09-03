"""Rescore all historical tokens using the current scoring formula.

Reads the features table, reconstructs feature dicts per (asset, decision_ts),
re-runs compute_scores() with the live code (including the proportional risk
fix), and updates the scores table in-place.  This is a one-time migration
tool — not part of the regular scoring pipeline.

Usage:
    python scripts/rescore.py [--dry-run] [--compare] [--min-change THRESHOLD]
                              [--limit N] [--symbol-filter SYM] [--top-pct P]
                              [--sweep] [--export-csv PATH]

Options:
    --dry-run          Compute scores but don't write to the database.
    --compare          Print old → new risk for each token (implies --dry-run).
    --min-change       Minimum |old - new| to include in --compare output
                       (default: 0, meaning all tokens).
    --limit N          Show/export only the top N movers by |delta|
                       (default: 50 console rows; CSV exports the full set).
    --symbol-filter S  Only review tokens whose symbol contains S
                       (case-insensitive; repeatable or comma-separated).
    --top-pct P        Keep only the top P% of movers by |delta|, e.g. 10 for
                       the top decile (implies --compare).
    --sweep            Print a mover-magnitude sweep — |delta| percentiles plus
                       mover counts at candidate --min-change thresholds — to
                       pick a cutoff before committing (implies --compare).
    --export-csv PATH  Write the filtered old → new → delta table to PATH
                       (implies --compare).

The filter flags (--limit / --symbol-filter / --top-pct) shape the diff
*review* only — the write pass always rescores every evaluated token, never a
subset, so a filtered review can't silently become a partial migration.

Pointing at a different database (integration tests / dry migrations):
    SERPENT_DB_PATH=/tmp/rescore.db python scripts/rescore.py --compare --dry-run
    DATABASE_URL=sqlite:////tmp/rescore.db python scripts/rescore.py --compare --dry-run

``Settings`` itself resolves ``SERPENT_DB_PATH`` (a SQLite file path or
``scheme://`` URL) and, failing that, ``DATABASE_URL`` into
``settings.database_url`` at construction — so every consumer
(``session_scope()`` users, alembic migrations, this CLI included) binds the
effective URL and the real ``__main__`` path can be driven end-to-end against
a throwaway DB.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging import get_logger
from scoring.formulas import compute_scores
from storage import models
from storage.database import session_scope

log = get_logger(__name__)


def _symbol_matches(symbol: str, filters: list[str]) -> bool:
    """Case-insensitive substring match against any of the review filters."""
    lowered = symbol.lower()
    return any(f.lower() in lowered for f in filters if f.strip())


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
    *,
    dry_run: bool = False,
    compare: bool = False,
    min_change: float = 0.0,
    limit: int | None = None,
    symbol_filters: list[str] | None = None,
    top_pct: float | None = None,
    sweep: bool = False,
    export_csv: str | None = None,
    session: Session | None = None,
) -> dict[str, int]:
    """Rescore all scored tokens and return stats.

    Args:
        dry_run:  Compute but don't write to the database.
        compare:  Print old → new risk for each token (implies dry_run).
        min_change: Minimum |old_risk - new_risk| to include in output.
        limit:  Cap the printed/exported mover table to the top N by |delta|.
        symbol_filters: Review-only substring filter on symbol (case-insensitive);
            never shrinks the write pass.
        top_pct:  Review-only: keep only the top P% of movers by |delta|, e.g.
            10 for the top decile (implies compare).
        sweep:  Print a mover-magnitude sweep (percentiles + candidate
            --min-change counts) to pick a cutoff; implies compare.
        export_csv: Write the filtered old → new → delta table to PATH; implies
            compare (and therefore dry run).
        session:  Optional session to run against (used by tests that need
            a controlled in-memory database); defaults to the configured DB.
    """

    def _run(active: Session) -> dict[str, int]:
        return _rescore_in_session(
            active,
            dry_run=dry_run,
            compare=compare,
            min_change=min_change,
            limit=limit,
            symbol_filters=symbol_filters,
            top_pct=top_pct,
            sweep=sweep,
            export_csv=export_csv,
        )

    # Normalize the review filters once so every entry point behaves the same:
    # a single filter may be comma-separated ("DOGE,WIF"), and empty tokens are
    # dropped ("pepe, " → ["pepe"]). Doing it here — not just in __main__ —
    # keeps the programmatic (injected-session) path on the same contract as
    # the CLI, so a comma-separated flag isn't a CLI-only convenience that
    # silently means "match a literal comma" for script callers.
    if symbol_filters:
        symbol_filters = [
            token.strip() for group in symbol_filters for token in group.split(",") if token.strip()
        ]
        if not symbol_filters:
            symbol_filters = None

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
    limit: int | None = None,
    symbol_filters: list[str] | None = None,
    top_pct: float | None = None,
    sweep: bool = False,
    export_csv: str | None = None,
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
    # (symbol, old, new, diff, asset_id, decision_ts) — review table + CSV rows.
    compare_rows: list[tuple[str, float, float, float, int, datetime]] = []

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
            if symbol_filters and not _symbol_matches(sym, symbol_filters):
                # Review filter: this continue can never prune a write because
                # compare forces dry_run=True above — the write block below only
                # runs when not dry_run. Keep that coupling explicit: if compare
                # ever stops implying dry-run, the filter would silently skip
                # writes for excluded tokens.
                continue
            compare_rows.append(
                (
                    sym,
                    score.risk,
                    result.risk,
                    result.risk - score.risk,
                    score.asset_id,
                    ts,
                )
            )

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
        if sweep:
            _print_mover_sweep(compare_rows)
        if top_pct is not None and compare_rows:
            keep = max(1, math.ceil(len(compare_rows) * top_pct / 100.0))
            compare_rows = compare_rows[:keep]
        show_cap = limit if limit is not None else 50
        print(f"\n--- Risk changes ({len(compare_rows)} tokens, min_change={min_change}) ---")
        print(f"{'Symbol':>20s}  {'Old':>8s}  {'New':>8s}  {'Delta':>8s}")
        print("-" * 52)
        for sym, old, new, diff, _asset_id, _ts in compare_rows[:show_cap]:
            print(f"{sym:>20s}  {old:>8.2f}  {new:>8.2f}  {diff:>+8.2f}")
        if len(compare_rows) > show_cap:
            print(
                f"  ... and {len(compare_rows) - show_cap} more (showing top {show_cap} by |delta|)"
            )
        if export_csv:
            csv_rows = compare_rows if limit is None else compare_rows[:limit]
            _export_compare_csv(export_csv, csv_rows)
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


def _print_mover_sweep(rows: list[tuple[str, float, float, float, int, datetime]]) -> None:
    """Print mover-magnitude stats to help pick a --min-change cutoff."""
    if not rows:
        print("\n--- Mover sweep (no movers above min_change=0) ---\n")
        return
    deltas = np.abs(np.array([r[3] for r in rows], dtype=float))
    p50 = float(np.percentile(deltas, 50))
    p90 = float(np.percentile(deltas, 90))
    p95 = float(np.percentile(deltas, 95))
    p99 = float(np.percentile(deltas, 99))
    print("\n--- Mover sweep (|delta| over the full mover set) ---")
    print(f"  movers: {len(deltas)}   p50={p50:.2f}  p90={p90:.2f}  p95={p95:.2f}  p99={p99:.2f}")
    print(f"  top 10% of movers sit at |delta| >= {p90:.2f}")
    print("  mover counts at candidate --min-change thresholds:")
    for thr in (1.0, 5.0, 10.0, 25.0, 50.0):
        print(f"    |delta| >= {thr:>5.1f}: {(deltas >= thr).sum():>7d} movers")
    print()


def _export_compare_csv(
    path: str, rows: list[tuple[str, float, float, float, int, datetime]]
) -> None:
    """Write the filtered review table (symbol → old → new → delta) to CSV."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "asset_id", "decision_ts", "old_risk", "new_risk", "delta"])
        for sym, old, new, diff, asset_id, ts in rows:
            writer.writerow(
                [sym, asset_id, ts.isoformat(), f"{old:.4f}", f"{new:.4f}", f"{diff:+.4f}"]
            )
    log.info("rescore_csv_exported", path=str(out), rows=len(rows))
    print(f"\nExported {len(rows)} mover rows to {out}\n")


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
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Show/export only the top N movers by |delta| (default: 50 console rows)",
    )
    parser.add_argument(
        "--symbol-filter",
        action="append",
        default=[],
        metavar="SYM",
        help="Only review tokens whose symbol contains SYM (case-insensitive; "
        "repeatable, or comma-separate several in one flag). Review-only.",
    )
    parser.add_argument(
        "--top-pct",
        type=float,
        default=None,
        help="Keep only the top P% of movers by |delta|, e.g. 10 for the top "
        "decile (implies --compare)",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Print a mover-magnitude sweep (percentiles + counts at candidate "
        "--min-change thresholds) to pick a cutoff; implies --compare",
    )
    parser.add_argument(
        "--export-csv",
        type=str,
        default=None,
        metavar="PATH",
        help="Write the filtered old -> new -> delta table to PATH; implies --compare",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.top_pct is not None and not 0 < args.top_pct <= 100:
        parser.error("--top-pct must be in (0, 100]")

    # Review-only flags imply --compare, which implies --dry-run: a write pass
    # requires a bare run (no --compare / --sweep / --export-csv / --top-pct).
    compare = args.compare or args.sweep or args.export_csv is not None or args.top_pct is not None
    # Comma-flattening, trimming, and empty-drop are owned by rescore() (single
    # source of truth, shared with the injected-session path) — pass through raw.
    rescore(
        dry_run=args.dry_run,
        compare=compare,
        min_change=args.min_change,
        limit=args.limit,
        symbol_filters=args.symbol_filter or None,
        top_pct=args.top_pct,
        sweep=args.sweep,
        export_csv=args.export_csv,
    )
