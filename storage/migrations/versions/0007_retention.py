"""retention autopilot schema: lake-growth tracking

Adds the ``retention_runs`` table, one row per retention-autopilot pass with
the Parquet lake totals (partitions, rows, bytes) and growth vs the previous
pass. Written defensively: revision 0001 creates the full current metadata, so
this migration only adds the delta on pre-existing databases.

Revision ID: 0007_retention
Revises: 0006_pump_physics
Create Date: 2026-08-19 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_retention"
down_revision = "0006_pump_physics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "retention_runs" not in tables:
        op.create_table(
            "retention_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("partitions", sa.Integer(), nullable=False),
            sa.Column("archived_rows", sa.Integer(), nullable=False),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            sa.Column("compacted", sa.Integer(), nullable=False),
            sa.Column("pruned", sa.Integer(), nullable=False),
            sa.Column("growth_bytes", sa.Integer(), nullable=False),
            sa.Column("growth_pct", sa.Float(), nullable=True),
            sa.Column("duration_sec", sa.Float(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "retention_runs" in tables:
        op.drop_table("retention_runs")
