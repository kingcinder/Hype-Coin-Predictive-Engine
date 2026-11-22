"""Covering index for /data/confidence label-generation progress.

``data_lake/labels.py::label_generation_progress`` probes the features
table with EXISTS on (asset_id, decision_ts) and needs missing_flag and
observed_at for the membership test. The existing
ix_features_asset_decision covers the seek, but each probe then faults
the table page for the remaining columns; on a cold cache the first
/data/confidence call took ~167s faulting ~4,400 random pages. This
covering index keeps the whole probe inside the index.

Build measured 2026-11-20 on a full live-DB copy: 27.8s, +~600MB on an
8.9M-row features table, 212G free on the volume — write-lock time
trivially acceptable with the engine stopped for the rescore+restart.

Revision ID: 0041_features_confidence_covering_index
Revises: 0040_ledger_entry_price_native
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_features_confidence_covering_index"
down_revision = "0040_ledger_entry_price_native"
branch_labels = None
depends_on = None

TABLE = "features"
INDEX = "ix_features_confidence_cover"


def upgrade() -> None:
    bind = op.get_bind()
    names = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:t"),
            {"t": TABLE},
        )
    }
    if INDEX not in names:
        op.create_index(INDEX, TABLE, ["asset_id", "decision_ts", "missing_flag", "observed_at"])


def downgrade() -> None:
    bind = op.get_bind()
    names = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:t"),
            {"t": TABLE},
        )
    }
    if INDEX in names:
        op.drop_index(INDEX, table_name=TABLE)
