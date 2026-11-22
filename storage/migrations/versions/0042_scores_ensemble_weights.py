"""Add ensemble_weights to scores.

The drift probe recomputes the engine's blended risk to compare against
the persisted distribution, but the ensemble recalibrates hourly from
accruing scorer accuracies: a probe in a fresh process loads weights
from the DB that differ from the weights that wrote the row, and a
~0.5-point shift amplifies into KS D ~ 0.5 on heavily-tied values
(2026-11-20: pinned-weight recomputation on the same 300-token window
gives EXACT 300/300, mean 0.0000 with write-time weights
rule 0.4515 / ml 0.156 / heuristic 0.3925, vs 0.5307 with DB-loaded
weights). Recording the write-time weights per row makes the probe
weight-stable: it re-blends with the stored weights when present and
falls back to the current weights when NULL.

Nullable JSON column: every pre-0042 row reads NULL and keeps the old
fallback behavior, so the migration is a pure add-column with no data
backfill (the historical weights are not recoverable, and the probe
samples only the latest window). Downgrade drops it.

Revision ID: 0042_scores_ensemble_weights
Revises: 0041_features_confidence_covering_index
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_scores_ensemble_weights"
down_revision = "0041_features_confidence_covering_index"
branch_labels = None
depends_on = None

TABLE = "scores"
COLUMN = "ensemble_weights"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}
    if COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}
    if COLUMN in columns:
        op.drop_column(TABLE, COLUMN)
