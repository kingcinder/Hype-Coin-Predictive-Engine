"""persist daily ntfy digest delivery state

Stores one digest row per UTC day so repeated scans and worker restarts do not
send duplicate daily summaries; failed deliveries remain retryable.

Revision ID: 0009_notification_digest
Revises: 0008_rpc_pool_snapshots
Create Date: 2026-08-20 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_notification_digest"
down_revision = "0008_rpc_pool_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "notification_digests" in tables:
        return
    op.create_table(
        "notification_digests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("digest_key", sa.String(length=32), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_count", sa.Integer(), nullable=False),
        sa.Column("ignition_count", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("digest_key", name="uq_notification_digest_key"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "notification_digests" in tables:
        op.drop_table("notification_digests")
