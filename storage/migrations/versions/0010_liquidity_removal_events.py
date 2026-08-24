"""persist on-chain LP burn and liquidity-removal events

Revision ID: 0010_liquidity_removal_events
Revises: 0009_notification_digest
Create Date: 2026-08-20 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_liquidity_removal_events"
down_revision = "0009_notification_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "liquidity_removal_events" in tables:
        return
    op.create_table(
        "liquidity_removal_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("pool_id", sa.Integer(), sa.ForeignKey("pools.id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("chain_slug", sa.String(length=32), nullable=False),
        sa.Column("event_kind", sa.String(length=64), nullable=False),
        sa.Column("tx_hash", sa.String(length=128), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "chain_slug",
            "tx_hash",
            "log_index",
            "event_kind",
            name="uq_liquidity_removal_chain_tx_log_kind",
        ),
    )
    op.create_index(
        "ix_liquidity_removal_asset_ts",
        "liquidity_removal_events",
        ["asset_id", "ts"],
    )
    op.create_index(
        "ix_liquidity_removal_observed_at",
        "liquidity_removal_events",
        ["observed_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "liquidity_removal_events" not in tables:
        return
    op.drop_index("ix_liquidity_removal_observed_at", table_name="liquidity_removal_events")
    op.drop_index("ix_liquidity_removal_asset_ts", table_name="liquidity_removal_events")
    op.drop_table("liquidity_removal_events")
