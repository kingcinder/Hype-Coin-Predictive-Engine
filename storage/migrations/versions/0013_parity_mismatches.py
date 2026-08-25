"""Persist parity mismatch history.

Adds ``parity_mismatches``: one row per lake-vs-SQL divergence recorded by a
parity run (run timestamp, comparison decision hour, asset, feature, SQL and
lake values, missing flags, run state), so operators can review divergence
history instead of only the latest ntfy page.

Revision ID: 0013_parity_mismatches
Revises: 0012_alert_ack
Create Date: 2026-08-24 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_parity_mismatches"
down_revision = "0012_alert_ack"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "parity_mismatches" not in tables:
        op.create_table(
            "parity_mismatches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("decision_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=True),
            sa.Column("symbol", sa.String(64), nullable=True),
            sa.Column("feature_name", sa.String(128), nullable=False),
            sa.Column("sql_value", sa.Float(), nullable=True),
            sa.Column("lake_value", sa.Float(), nullable=True),
            sa.Column("sql_missing", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("lake_missing", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("state", sa.String(16), nullable=False, server_default="red"),
        )
        op.create_index("ix_parity_mismatch_run_ts", "parity_mismatches", ["run_ts"])
        op.create_index("ix_parity_mismatch_decision_ts", "parity_mismatches", ["decision_ts"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "parity_mismatches" in tables:
        op.drop_table("parity_mismatches")
