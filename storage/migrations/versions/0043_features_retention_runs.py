"""Add feature_retention_runs table for the features-table pruner.

The features-table retention pass (``ops.features_retention``) records each
pass here -- rows before/after, rows pruned, keys kept, duration, dry-run
flag, and any error -- so Feed Health can report the table's trajectory
and the ``--check-due`` gate can tell whether a pass is due. Pure
add-table migration with no data backfill; downgrade drops the table.

Revision ID: 0043_features_retention_runs
Revises: 0042_scores_ensemble_weights
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_features_retention_runs"
down_revision = "0042_scores_ensemble_weights"
branch_labels = None
depends_on = None

TABLE = "feature_retention_runs"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rows_before", sa.Integer(), nullable=False),
        sa.Column("rows_after", sa.Integer(), nullable=False),
        sa.Column("rows_pruned", sa.Integer(), nullable=False),
        sa.Column("keys_kept", sa.Integer(), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE in sa.inspect(bind).get_table_names():
        op.drop_table(TABLE)
