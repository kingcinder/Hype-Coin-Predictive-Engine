"""Features-table retention: prune feature rows the consumers no longer need.

The features table is the fastest-growing table in the DB: one row per
(asset_id, decision_ts, feature_name), ~24M rows/day at full write rate
(~8,300 assets x ~75 cycles/day x ~43 features), while the trainer pool is
only 2,288 labeled samples. An arbitrary day-count cutoff would silently
destroy training data, so the policy is **label-anchored**: a row is kept
only if its (asset_id, decision_ts) key is still needed by a consumer.

Keep-set -- a key is kept if ANY rule matches:

1. **Labeled** -- the key has >= 1 row in ``labels`` (any age). The trainer
   (``forecast/engine.py`` ``_collect_samples``) reads features ONLY at keys
   that have BOTH ignition and collapse labels; pruning a labeled key would
   silently shrink the training pool. Labels persist in the ``labels``
   table, so a pruned feature row at a labeled key is unrecoverable.
2. **Pending** -- ``decision_ts`` within ``FEATURES_RETENTION_PENDING_HOURS``
   (24h) of the newest decision_ts AND an entry price exists (the label
   seeder's ``_labelable_at`` entry test: a market snapshot at or before the
   point, observed by then, with a positive price). The 24h forward label
   window (``forecast_forward_hours``) has not resolved yet, so the daily
   retrain's label seeder may still label this point.
3. **Resolving** -- ``decision_ts`` in [pending, resolving) (24h, 72h) AND
   an entry price AND >= 1 future snapshot inside the 24h forward window
   (the seeder's ``_forward_prices`` test). The window has resolved but the
   daily retrain's seeder may not have run since; 72h gives two
   missed-retrain days of slack. In practice this rule retains ~0 rows
   (the seeder labels everything labelable on its daily run); it is a
   safety net for retrain/prune timing skew, not a data set.
4. **Latest** -- one of the latest ``FEATURES_RETENTION_LATEST_PER_ASSET``
   (2) decision_ts per asset. The drift probe samples the single latest
   decision_ts, ``scoring.engine._changed_features`` needs the previous
   one, and the API reads latest-per-asset.
6. **Mission horizon** -- ``decision_ts`` within
   ``features_retention_mission_horizon_hours`` (168h) of ``as_of`` AND an
   entry price AND >= 1 future snapshot inside the 24h forward window
   (the same labelable test as Rule 3, but with NO labels-existence
   condition and a far longer horizon). The mission label types in
   ``MISSION_LABEL_TYPES`` do not all resolve inside the pending +
   resolving windows: forward labels resolve inside 24h, but harvest
   labels resolve only when an episode completes -- well past 72h.
   Without this rule the first retention pass after the mission labels
   land would prune the feature rows those labels will need, before the
   labels exist.

Everything else is pruned, in id-range batches of
``FEATURES_RETENTION_BATCH_SIZE`` rows, so a pass never holds a long write
lock against the live engine. Two guards keep the pass safe while the
engine is running:

- The keep-set is built in a TEMP table on a single connection, and the
  DELETE is restricted to ``id <= max_id`` captured at build time: rows
  the engine writes mid-pass are never touched.
- Windows are anchored to the newest decision_ts in the table (``as_of``),
  not wall-clock, so a prune after an engine outage does not nuke the
  recent window just because the clock moved.

Known limitations (accepted, documented):

- ``scripts/rescore.py`` loads the ENTIRE features table; after pruning it
  can only rescore retained rows. Same for the leakage audit's feature
  join (it degrades gracefully to NaN) and ``api/analytics.py``
  ``similar_setups`` (its reach is limited to retained history). These are
  manual/analytical tools, not the trainer or the live paths.
- The policy assumes the label seeder runs at least every ~48h (it runs
  as bootstrap inside the daily forecast retrain). If retrains stall for
  longer than the resolving window, some labelable-but-unlabeled points
  are pruned and their future labels are lost; the loss is bounded to the
  points that would have been labeled in that window.

Driven the same way as the lake retention (``ops.retention``):

- ``python -m ops.features_retention --once`` (one pass; ``--dry-run``
  counts what would be pruned without deleting).
- ``python -m ops.features_retention --check-due`` exits 0 when a pass is
  due under ``FEATURES_RETENTION_CADENCE_HOURS``, 1 otherwise, so a
  systemd timer (``deploy/systemd/serpent-features-retention.timer``) can
  gate on it.

A failed pass records ``component:features_retention`` health as red and
returns ``{"error": ...}``, mirroring the lake retention's degradation
contract. The pass NEVER touches the live engine process, ports 8000/8501,
or any table other than ``features`` (plus its own run record).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

KEEP_TABLE = "_features_retention_keep"
COMPONENT = "features_retention"

# The mission label types (spec §5) whose feature keys Rule 6 protects
# before the labels exist. Tasks 2 and 7 define the label constants with
# these exact strings; tests/test_features_retention.py pins the overlap.
MISSION_LABEL_TYPES: tuple[str, ...] = (
    "forward_max_gain_pct",
    "forward_max_drawdown_pct",
    "hours_to_peak",
    "hours_to_trough",
    "executable_peak_gain_s02",
    "executable_peak_gain_s10",
    "executable_peak_gain_s25",
    "harvest_window_start_hours",
    "collapse_confirmed",
    "second_leg",
)


def _entry_exists_sql(alias: str = "f") -> str:
    """The label seeder's ``_labelable_at`` entry test, verbatim in SQL.

    A market snapshot for the asset's pair, at or before the point,
    observed by then, with a positive price -- i.e. the point has an entry
    price the seeder can label against.
    """
    return (
        f"EXISTS (SELECT 1 FROM pairs p "
        f"JOIN market_snapshots ms ON ms.pair_id = p.id "
        f"WHERE p.base_asset_id = {alias}.asset_id "
        f"AND ms.ts <= {alias}.decision_ts "
        f"AND ms.observed_at <= {alias}.decision_ts "
        f"AND ms.price_usd IS NOT NULL AND ms.price_usd > 0)"
    )


def _future_exists_sql(alias: str = "f", *, forward_hours: int) -> str:
    """The label seeder's ``_forward_prices`` test, verbatim in SQL.

    >= 1 market snapshot for the asset's pair inside the forward window
    (decision_ts, decision_ts + forward_hours], observed by the prune's
    as-of. Both sides are normalised through ``datetime()`` so the
    comparison is on bare UTC seconds, not on stored strings whose
    timezone-offset suffix would sort after the bare form at the exact
    window boundary.
    """
    return (
        f"EXISTS (SELECT 1 FROM pairs p2 "
        f"JOIN market_snapshots ms2 ON ms2.pair_id = p2.id "
        f"WHERE p2.base_asset_id = {alias}.asset_id "
        f"AND datetime(ms2.ts) > datetime({alias}.decision_ts) "
        f"AND datetime(ms2.ts) <= datetime({alias}.decision_ts, '+{forward_hours} hours') "
        f"AND ms2.observed_at <= :as_of "
        f"AND ms2.price_usd IS NOT NULL AND ms2.price_usd > 0)"
    )


def build_keep_keys(conn: Connection, *, settings: Settings, as_of: str) -> int:
    """Build the keep-set TEMP table for this pass; return the key count.

    The table holds the (asset_id, decision_ts) keys of every feature row
    the consumers still need, per the rules in the module docstring.
    Keys are stored exactly as they appear in ``features`` (raw stored
    strings), so the DELETE's ``NOT EXISTS`` match is an exact equality.
    """
    conn.execute(text(f"DROP TABLE IF EXISTS {KEEP_TABLE}"))
    conn.execute(
        text(
            f"CREATE TEMP TABLE {KEEP_TABLE} ("
            "asset_id INTEGER NOT NULL, "
            "decision_ts TEXT NOT NULL, "
            "PRIMARY KEY (asset_id, decision_ts))"
        )
    )
    forward_hours = int(settings.forecast_forward_hours)
    pending = float(settings.features_retention_pending_hours)
    resolving = float(settings.features_retention_resolving_hours)
    latest_n = int(settings.features_retention_latest_per_asset)
    params = {"as_of": as_of}

    # Rule 1 -- labeled keys, any age. The trainer's pool.
    conn.execute(
        text(
            f"INSERT OR IGNORE INTO {KEEP_TABLE} (asset_id, decision_ts) "
            "SELECT DISTINCT asset_id, ts FROM labels"
        )
    )
    # Rule 2 -- pending: inside the pending window, with an entry price.
    conn.execute(
        text(
            f"INSERT OR IGNORE INTO {KEEP_TABLE} (asset_id, decision_ts) "
            "SELECT DISTINCT f.asset_id, f.decision_ts FROM features f "
            "WHERE datetime(f.decision_ts) >= datetime(:as_of, '-"
            f"{pending:g} hours') "
            f"AND {_entry_exists_sql('f')}"
        ),
        params,
    )
    # Rule 3 -- resolving: window resolved, entry + future prices present,
    # awaiting the daily retrain's label seeder.
    conn.execute(
        text(
            f"INSERT OR IGNORE INTO {KEEP_TABLE} (asset_id, decision_ts) "
            "SELECT DISTINCT f.asset_id, f.decision_ts FROM features f "
            "WHERE datetime(f.decision_ts) < datetime(:as_of, '-"
            f"{pending:g} hours') "
            "AND datetime(f.decision_ts) >= datetime(:as_of, '-"
            f"{resolving:g} hours') "
            f"AND {_entry_exists_sql('f')} "
            f"AND {_future_exists_sql('f', forward_hours=forward_hours)} "
            "AND NOT EXISTS (SELECT 1 FROM labels l "
            "WHERE l.asset_id = f.asset_id AND l.ts = f.decision_ts)"
        ),
        params,
    )
    # Rule 4 -- latest N decision_ts per asset (drift probe + _changed_features).
    conn.execute(
        text(
            f"INSERT OR IGNORE INTO {KEEP_TABLE} (asset_id, decision_ts) "
            "SELECT asset_id, decision_ts FROM ("
            "SELECT asset_id, decision_ts, "
            "ROW_NUMBER() OVER (PARTITION BY asset_id ORDER BY decision_ts DESC) AS rn "
            "FROM (SELECT DISTINCT asset_id, decision_ts FROM features)"
            ") WHERE rn <= :latest_n"
        ),
        {"latest_n": latest_n},
    )
    # Rule 5 -- risk-outcome keys, any age.  Forward validation joins
    # features to risk_outcomes on (asset_id, scored_at); pruning a feature
    # row at an outcome key destroys validation coverage (Nov 22 prune:
    # reference coverage 118 -> 4, live-stratum coverage 7.8%).  The
    # timestamp formats differ (features.decision_ts is text,
    # risk_outcomes.scored_at is a datetime), so compare at second
    # precision via datetime() -- a superset, which is safe for a keep rule.
    conn.execute(
        text(
            f"INSERT OR IGNORE INTO {KEEP_TABLE} (asset_id, decision_ts) "
            "SELECT DISTINCT f.asset_id, f.decision_ts FROM features f "
            "WHERE EXISTS (SELECT 1 FROM risk_outcomes r "
            "WHERE r.asset_id = f.asset_id "
            "AND datetime(r.scored_at) = datetime(f.decision_ts))"
        )
    )
    # Rule 6 -- mission horizon: labelable keys (entry + >= 1 forward
    # price, the Rule 3 tests) younger than the mission horizon are kept
    # even with NO row in labels yet. Harvest labels resolve only when an
    # episode completes, well past the resolving window; without this
    # rule the pass would prune the feature rows the harvest labels will
    # need before those labels exist. Unlike Rule 3 there is deliberately
    # no labels-existence condition and no lower age bound.
    mission = float(settings.features_retention_mission_horizon_hours)
    conn.execute(
        text(
            f"INSERT OR IGNORE INTO {KEEP_TABLE} (asset_id, decision_ts) "
            "SELECT DISTINCT f.asset_id, f.decision_ts FROM features f "
            "WHERE datetime(f.decision_ts) >= datetime(:as_of, '-"
            f"{mission:g} hours') "
            f"AND {_entry_exists_sql('f')} "
            f"AND {_future_exists_sql('f', forward_hours=forward_hours)}"
        ),
        params,
    )
    keys_kept = conn.execute(text(f"SELECT COUNT(*) FROM {KEEP_TABLE}")).scalar_one()
    log.info("features_retention keep-set built keys_kept=%s as_of=%s", keys_kept, as_of)
    return int(keys_kept)


def _prunable_clause(*, max_id: int) -> str:
    """Rows eligible for pruning: existed at build time, key not kept."""
    return (
        f"id <= {max_id} AND NOT EXISTS (SELECT 1 FROM {KEEP_TABLE} k "
        "WHERE k.asset_id = features.asset_id "
        "AND k.decision_ts = features.decision_ts)"
    )


def prune_features(
    conn: Connection,
    *,
    settings: Settings | None = None,
    dry_run: bool = False,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Run one features-retention pass on ``conn``; return the result dict.

    Builds the keep-set, then deletes prunable rows in id-range batches,
    committing after each batch. ``max_rows`` caps the rows deleted in this
    pass (None = the ``FEATURES_RETENTION_MAX_ROWS_PER_PASS`` setting;
    0 = unlimited). In dry-run mode nothing is deleted; the result carries
    the would-be count in ``rows_pruned`` with ``dry_run=True``.
    """
    settings = settings or get_settings()
    started = monotonic()
    as_of = conn.execute(text("SELECT MAX(decision_ts) FROM features")).scalar()
    if as_of is None:
        return {
            "rows_before": 0,
            "rows_after": 0,
            "rows_pruned": 0,
            "keys_kept": 0,
            "dry_run": dry_run,
            "duration_sec": 0.0,
        }
    rows_before = conn.execute(text("SELECT COUNT(*) FROM features")).scalar_one()
    id_min, id_max = conn.execute(text("SELECT MIN(id), MAX(id) FROM features")).one()
    keys_kept = build_keep_keys(conn, settings=settings, as_of=str(as_of))
    conn.commit()

    cap = settings.features_retention_max_rows_per_pass if max_rows is None else max_rows
    cap = int(cap)
    batch = int(settings.features_retention_batch_size)
    clause = _prunable_clause(max_id=int(id_max))

    if dry_run:
        would_prune = conn.execute(
            text(f"SELECT COUNT(*) FROM features WHERE {clause}")
        ).scalar_one()
        conn.execute(text(f"DROP TABLE IF EXISTS {KEEP_TABLE}"))
        conn.commit()
        return {
            "rows_before": int(rows_before),
            "rows_after": int(rows_before),
            "rows_pruned": int(would_prune),
            "keys_kept": int(keys_kept),
            "dry_run": True,
            "duration_sec": round(monotonic() - started, 3),
        }

    deleted = 0
    lo = int(id_min)
    while lo <= int(id_max):
        if cap > 0 and deleted >= cap:
            break
        # Narrow the batch to the remaining allowance so one batch can
        # never overshoot the cap: a batch of N ids holds at most N rows.
        width = batch
        if cap > 0:
            width = min(batch, cap - deleted)
        hi = min(lo + width - 1, int(id_max))
        result = conn.execute(
            text(f"DELETE FROM features WHERE id BETWEEN :lo AND :hi AND {clause}"),
            {"lo": lo, "hi": hi},
        )
        conn.commit()
        deleted += int(result.rowcount or 0)
        lo = hi + 1
    rows_after = conn.execute(text("SELECT COUNT(*) FROM features")).scalar_one()
    conn.execute(text(f"DROP TABLE IF EXISTS {KEEP_TABLE}"))
    conn.commit()
    log.info(
        "features_retention pass done pruned=%s rows %s->%s keys_kept=%s",
        deleted,
        rows_before,
        rows_after,
        keys_kept,
    )
    return {
        "rows_before": int(rows_before),
        "rows_after": int(rows_after),
        "rows_pruned": int(deleted),
        "keys_kept": int(keys_kept),
        "dry_run": False,
        "duration_sec": round(monotonic() - started, 3),
    }


def _latest_run(session: Session) -> models.FeatureRetentionRun | None:
    return session.scalar(
        select(models.FeatureRetentionRun).order_by(models.FeatureRetentionRun.ts.desc()).limit(1)
    )


def features_retention_due(
    session: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> bool:
    """True when the cadence since the last features-retention pass elapsed.

    A disabled policy is never due; with no recorded pass the pass is due
    (first run). Mirrors ``ops.retention.retention_due``.
    """
    settings = settings or get_settings()
    if not settings.features_retention_enabled:
        return False
    now = ensure_utc(now or utc_now())
    latest = _latest_run(session)
    if latest is None:
        return True
    return now - ensure_utc(latest.ts) >= timedelta(
        hours=float(settings.features_retention_cadence_hours)
    )


def run_features_retention(
    engine: Engine,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Run one features-retention pass against ``engine``; return the result.

    The engine is a required argument (no module-global default) so a test
    or lab run can never silently bind the live DB: the CLI passes the
    configured engine explicitly. The pass itself runs on a dedicated
    connection; the run record + health row are written afterwards on a
    short-lived session. A failed pass records red health and returns
    ``{"error": ...}`` instead of raising, mirroring ``ops.retention``.
    """
    settings = settings or get_settings()
    started = monotonic()
    try:
        with engine.connect() as conn:
            result = prune_features(conn, settings=settings, dry_run=dry_run, max_rows=max_rows)
    except Exception as exc:  # noqa: BLE001 - record + report, never raise
        result = {
            "rows_before": 0,
            "rows_after": 0,
            "rows_pruned": 0,
            "keys_kept": 0,
            "dry_run": dry_run,
            "duration_sec": round(monotonic() - started, 3),
            "error": str(exc),
        }
        log.exception("features_retention pass failed")
    result["duration_sec"] = round(monotonic() - started, 3)
    with Session(engine) as session:
        session.add(
            models.FeatureRetentionRun(
                ts=utc_now(),
                rows_before=int(result["rows_before"]),
                rows_after=int(result["rows_after"]),
                rows_pruned=int(result["rows_pruned"]),
                keys_kept=int(result["keys_kept"]),
                duration_sec=float(result["duration_sec"]),
                dry_run=bool(dry_run),
                error=result.get("error"),
            )
        )
        record_health(
            session,
            component=COMPONENT,
            state="red" if result.get("error") else "ok",
            message=(
                f"pruned={result['rows_pruned']} kept_keys={result['keys_kept']} "
                f"rows {result['rows_before']}->{result['rows_after']} "
                f"dry_run={dry_run}" + (f" error={result['error']}" if result.get("error") else "")
            ),
        )
        session.commit()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Serpent Circle features-table retention: prune feature rows the trainer, "
            "validation harness, and live scoring no longer need"
        )
    )
    parser.add_argument("--once", action="store_true", help="run one retention pass and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would be pruned without deleting anything",
    )
    parser.add_argument(
        "--check-due",
        action="store_true",
        help="exit 0 when a retention pass is due, 1 otherwise",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help=(
            "cap rows deleted in this pass "
            "(0 = unlimited; default: FEATURES_RETENTION_MAX_ROWS_PER_PASS)"
        ),
    )
    args = parser.parse_args()

    from storage.database import SessionLocal, engine  # noqa: PLC0415 - CLI entrypoint

    if args.check_due:
        with SessionLocal() as session:
            due = features_retention_due(session)
        print("due" if due else "not-due")
        raise SystemExit(0 if due else 1)

    if args.once:
        settings = get_settings()
        print(
            f"features retention enabled={settings.features_retention_enabled} "
            f"cadence_hours={settings.features_retention_cadence_hours} "
            f"pending_hours={settings.features_retention_pending_hours} "
            f"resolving_hours={settings.features_retention_resolving_hours} "
            f"latest_per_asset={settings.features_retention_latest_per_asset} "
            f"dry_run={args.dry_run}"
        )
        result = run_features_retention(
            engine, dry_run=args.dry_run, settings=settings, max_rows=args.max_rows
        )
        print(json.dumps(result, default=str))
        if result.get("error"):
            raise SystemExit(2)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
