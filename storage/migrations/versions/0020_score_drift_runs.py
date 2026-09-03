"""score_drift_runs: per-probe drift measurements for the trend series

Adds ``score_drift_runs``: one row per drift probe that produced a comparable
sample (pure-numpy KS D/p, persisted-vs-live distinct-value quantization
ratio, mean per-token |delta|, plus state/sample counts), so the divergence is
visible growing over time instead of only the latest ntfy page / health row.

Revision ID: 0020_score_drift_runs
Revises: 0019_ml_probability_thresholds
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_score_drift_runs"
down_revision = "0019_ml_probability_thresholds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "score_drift_runs" not in tables:
        op.create_table(
            "score_drift_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("sampled", sa.Integer(), nullable=False),
            sa.Column("compared", sa.Integer(), nullable=False),
            sa.Column("ks_d", sa.Float(), nullable=False),
            sa.Column("ks_p", sa.Float(), nullable=False),
            sa.Column("distinct_ratio", sa.Float(), nullable=False),
            sa.Column("mean_abs_delta", sa.Float(), nullable=False),
            sa.Column("distinct_persisted", sa.Integer(), nullable=False),
            sa.Column("distinct_live", sa.Integer(), nullable=False),
            sa.Column("no_features", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("message", sa.Text(), nullable=False),
        )
        op.create_index("ix_score_drift_runs_run_ts", "score_drift_runs", ["run_ts"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "score_drift_runs" in tables:
        op.drop_table("score_drift_runs")
