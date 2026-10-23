"""Persist point-in-time Solana mint and LP safety evidence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_solana_safety_snapshots"
down_revision = "0021_archive_manifest_sha256"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "solana_safety_snapshots" not in tables:
        op.create_table(
            "solana_safety_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
        )
        op.create_index(
            "ix_solana_safety_asset_observed",
            "solana_safety_snapshots",
            ["asset_id", "observed_at"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "solana_safety_snapshots" in tables:
        op.drop_index("ix_solana_safety_asset_observed", table_name="solana_safety_snapshots")
        op.drop_table("solana_safety_snapshots")
