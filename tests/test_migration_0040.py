"""Migration 0040: add entry_price_native to ledger_open_positions.

Upgrade adds a nullable Numeric column; downgrade drops it; both are
idempotent. The table is seeded with only the columns the migration
inspects, mirroring the 0039 test pattern.
"""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

REVISION = "0040_ledger_entry_price_native"


def _load_migration():
    return importlib.import_module(f"storage.migrations.versions.{REVISION}")


def _seeded_engine():
    metadata = sa.MetaData()
    sa.Table(
        "ledger_open_positions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("qty", sa.Numeric(38, 12), nullable=False),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    return engine


def _run_with_context(engine, fn):
    conn = engine.connect()
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        fn()
    return conn


def _columns(engine):
    with engine.connect() as conn:
        return {c["name"] for c in sa.inspect(conn).get_columns("ledger_open_positions")}


def test_migration_0040_adds_nullable_column() -> None:
    migration = _load_migration()
    assert migration.revision == REVISION
    assert migration.down_revision == "0039_feature_observed_at_evidence_time"
    engine = _seeded_engine()
    assert "entry_price_native" not in _columns(engine)
    conn = _run_with_context(engine, migration.upgrade)
    conn.close()
    assert "entry_price_native" in _columns(engine)
    with engine.connect() as conn:
        col = next(
            c
            for c in sa.inspect(conn).get_columns("ledger_open_positions")
            if c["name"] == "entry_price_native"
        )
    assert col["nullable"] is True


def test_migration_0040_is_idempotent_and_reversible() -> None:
    migration = _load_migration()
    engine = _seeded_engine()
    conn = _run_with_context(engine, migration.upgrade)
    conn.close()
    conn = _run_with_context(engine, migration.upgrade)
    conn.close()
    assert "entry_price_native" in _columns(engine)
    conn = _run_with_context(engine, migration.downgrade)
    conn.close()
    assert "entry_price_native" not in _columns(engine)
