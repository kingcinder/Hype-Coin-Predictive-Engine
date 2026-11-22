"""Migration 0039: re-stamp lifecycle_phase observed_at with evidence time.

Only rows whose stored value equals the point-in-time recomputation
from lifecycle events are re-stamped; a genuinely wrong value, a
missing row, a row with no determining event, and every other feature
are left untouched.  Each case uses its own asset, respecting the real
(asset_id, decision_ts, feature_name) uniqueness.
"""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

REVISION = "0039_feature_observed_at_evidence_time"

# Decision time shared by every seeded row.
DECISION = "2026-05-01 12:00:00.000000"
# Determining event (latest by ts, observed_at <= decision): parabolic.
EVENT_TS = "2026-05-01 11:00:00.000000"
EVENT_OBSERVED = "2026-05-01 11:30:00.000000"
# An earlier event, superseded by the determining one.
EARLIER_TS = "2026-05-01 10:00:00.000000"
EARLIER_OBSERVED = "2026-05-01 10:05:00.000000"
# Backfill write time (after the decision): the provenance artifact.
BACKFILL = "2026-05-02 00:00:00.000000"


def _load_migration():
    return importlib.import_module(f"storage.migrations.versions.{REVISION}")


def _seeded_engine():
    metadata = sa.MetaData()
    sa.Table(
        "features",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("asset_id", sa.Integer, nullable=False),
        sa.Column("decision_ts", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.String(32), nullable=False),
        sa.Column("feature_name", sa.String(128), nullable=False),
        sa.Column("feature_value", sa.Float, nullable=False),
        sa.Column("missing_flag", sa.Boolean, nullable=False),
    )
    sa.Table(
        "lifecycle_events",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("asset_id", sa.Integer, nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("ts", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.String(32), nullable=False),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as conn:
        # Assets 1, 2, 3, 5 share the same two events; asset 4 has none.
        for asset_id in (1, 2, 3, 5):
            conn.execute(
                sa.text(
                    "INSERT INTO lifecycle_events (asset_id, phase, ts, observed_at) VALUES "
                    "(:a, 'ignition', :t1, :o1), (:a, 'parabolic', :t2, :o2)"
                ),
                {
                    "a": asset_id,
                    "t1": EARLIER_TS,
                    "o1": EARLIER_OBSERVED,
                    "t2": EVENT_TS,
                    "o2": EVENT_OBSERVED,
                },
            )
        # A (asset 1): value matches the recomputation (parabolic = 2) -> re-stamped.
        # B (asset 2): value does not match (collapse = 4) -> untouched.
        # C (asset 3): missing row -> untouched.
        # D (asset 4): no determining event -> untouched.
        # E (asset 1): a different feature -> untouched.
        # F (asset 5): already stamped with evidence time -> unchanged (idempotent).
        conn.execute(
            sa.text(
                "INSERT INTO features "
                "(asset_id, decision_ts, observed_at, feature_name, feature_value, missing_flag) "
                "VALUES "
                "(1, :d, :b, 'lifecycle_phase', 2.0, 0), "
                "(2, :d, :b, 'lifecycle_phase', 4.0, 0), "
                "(3, :d, :b, 'lifecycle_phase', 0.0, 1), "
                "(4, :d, :b, 'lifecycle_phase', 2.0, 0), "
                "(1, :d, :b, 'holder_count', 5.0, 0), "
                "(5, :d, :e, 'lifecycle_phase', 2.0, 0)"
            ),
            {"d": DECISION, "b": BACKFILL, "e": EVENT_OBSERVED},
        )
    return engine


def _run_upgrade(engine):
    """Run the migration's upgrade() with the alembic ``op`` proxy installed.

    ``Operations.context`` installs the module-level ``op`` proxy that the
    migration file uses, so ``upgrade()`` runs exactly as it does under
    ``alembic upgrade head``.
    """
    conn = engine.connect()
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        _load_migration().upgrade()
    return conn


def test_migration_0039_restamps_only_matching_rows() -> None:
    migration = _load_migration()
    assert migration.revision == REVISION
    assert migration.down_revision == "0038_trading_timestamps"

    conn = _run_upgrade(_seeded_engine())
    rows = conn.execute(
        sa.text(
            "SELECT asset_id, feature_name, feature_value, missing_flag, observed_at "
            "FROM features ORDER BY id"
        )
    ).fetchall()
    conn.close()
    # A re-stamped to the determining event's observed_at; value unchanged.
    assert rows[0] == (1, "lifecycle_phase", 2.0, 0, EVENT_OBSERVED)
    # B mismatched value: untouched.
    assert rows[1] == (2, "lifecycle_phase", 4.0, 0, BACKFILL)
    # C missing: untouched.
    assert rows[2] == (3, "lifecycle_phase", 0.0, 1, BACKFILL)
    # D no determining event: untouched.
    assert rows[3] == (4, "lifecycle_phase", 2.0, 0, BACKFILL)
    # E other feature: untouched.
    assert rows[4] == (1, "holder_count", 5.0, 0, BACKFILL)
    # F already evidence-stamped: unchanged.
    assert rows[5] == (5, "lifecycle_phase", 2.0, 0, EVENT_OBSERVED)


def test_migration_0039_is_idempotent() -> None:
    engine = _seeded_engine()
    conn = _run_upgrade(engine)
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        _load_migration().upgrade()
    rows = conn.execute(
        sa.text(
            "SELECT observed_at FROM features WHERE feature_name = 'lifecycle_phase' "
            "AND missing_flag = 0 ORDER BY id"
        )
    ).fetchall()
    conn.close()
    assert rows == [
        (EVENT_OBSERVED,),  # A
        (BACKFILL,),  # B
        (BACKFILL,),  # D
        (EVENT_OBSERVED,),  # F
    ]
