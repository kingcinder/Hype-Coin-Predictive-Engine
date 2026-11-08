"""Persist independent market analysis evidence and narrative."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_independent_market_analysis"
down_revision = "0023_risk_confidence_calibration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "market_analysis_runs" not in tables:
        op.create_table(
            "market_analysis_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("llm_model", sa.String(128)),
            sa.Column("narrative_status", sa.String(32), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
            sa.Column("narrative", sa.Text(), nullable=False),
            sa.Column("risk_assessment", sa.Text(), nullable=False),
        )
        op.create_index(
            "ix_market_analysis_asset_generated",
            "market_analysis_runs",
            ["asset_id", "generated_at"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "market_analysis_runs" in tables:
        op.drop_index("ix_market_analysis_asset_generated", table_name="market_analysis_runs")
        op.drop_table("market_analysis_runs")
