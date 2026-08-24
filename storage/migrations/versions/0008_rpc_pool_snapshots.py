"""persist per-endpoint RPC pool snapshots

Stores the worker's endpoint state at the end of each scan so the API can
render accurate RPC Pool Status across separate processes.

Revision ID: 0008_rpc_pool_snapshots
Revises: 0007_retention
Create Date: 2026-08-20 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_rpc_pool_snapshots"
down_revision = "0007_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "rpc_pool_snapshots" in tables:
        return
    op.create_table(
        "rpc_pool_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain_slug", sa.String(length=32), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("health", sa.Float(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("down", sa.Boolean(), nullable=False),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_ok", sa.Boolean(), nullable=True),
        sa.Column("probe_count", sa.Integer(), nullable=False),
        sa.Column("probe_successes", sa.Integer(), nullable=False),
        sa.Column("probe_failures", sa.Integer(), nullable=False),
        sa.Column("probe_history", sa.JSON(), nullable=False),
        sa.UniqueConstraint("chain_slug", "url", "ts", name="uq_rpc_pool_snapshot"),
    )
    op.create_index(
        "ix_rpc_pool_snapshots_chain_ts", "rpc_pool_snapshots", ["chain_slug", "ts"]
    )
    op.create_index("ix_rpc_pool_snapshots_url_ts", "rpc_pool_snapshots", ["url", "ts"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "rpc_pool_snapshots" not in tables:
        return
    op.drop_index("ix_rpc_pool_snapshots_url_ts", table_name="rpc_pool_snapshots")
    op.drop_index("ix_rpc_pool_snapshots_chain_ts", table_name="rpc_pool_snapshots")
    op.drop_table("rpc_pool_snapshots")
