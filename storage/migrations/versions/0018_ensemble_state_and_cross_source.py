"""Add EnsembleState and CrossSourceSignal tables.

Phase 7 — Intelligence Multiplier:
- EnsembleState: persists adaptive ensemble weights and scorer accuracy
  across process restarts so learned weights accumulate over time.
- CrossSourceSignal: records cross-source signal fusion results per asset,
  tracking how many independent sources corroborate a signal and the
  resulting confidence boost.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "ensemble_state" not in tables:
        op.create_table(
            "ensemble_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("current_weights", sa.JSON(), nullable=False),
            sa.Column("scorer_accuracy", sa.JSON(), nullable=False),
            sa.Column(
                "calibration_buckets", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
            ),
            sa.Column("weight_history", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column(
                "total_predictions", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column("last_recalibrated_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "cross_source_signals" not in tables:
        op.create_table(
            "cross_source_signals",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("source_count", sa.Integer(), nullable=False),
            sa.Column("sources", sa.JSON(), nullable=False),
            sa.Column("fusion_score", sa.Float(), nullable=False),
            sa.Column("confidence_boost", sa.Float(), nullable=False),
            sa.Column("signal_agreement", sa.Float(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_cross_source_signals_asset_ts",
            "cross_source_signals",
            ["asset_id", "observed_at"],
        )
        op.create_index(
            "ix_cross_source_signals_source_count",
            "cross_source_signals",
            ["source_count"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "cross_source_signals" in tables:
        op.drop_table("cross_source_signals")
    if "ensemble_state" in tables:
        op.drop_table("ensemble_state")
