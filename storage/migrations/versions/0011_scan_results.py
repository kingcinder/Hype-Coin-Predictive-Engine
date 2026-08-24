"""Persist ingestion scan pipeline stage counts.

Revision ID: 0011_scan_results
Revises: 0010_liquidity_removal_events
Create Date: 2026-08-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_scan_results"
down_revision = "0010_liquidity_removal_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("pairs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profiles", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scores", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ignition_events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fingerprints", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("forecasts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lifecycle", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("narrative", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mempool", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lp_removals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prelaunch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("catalysts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archive", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ntfy_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rpc_pool_notifications", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rpc_pool_snapshots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(32), nullable=False, server_default="ok"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_scan_results_ts", "scan_results", ["ts"])


def downgrade() -> None:
    op.drop_table("scan_results")
