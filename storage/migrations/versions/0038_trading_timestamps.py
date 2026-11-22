"""Add TimestampMixin columns to the 0030 trading tables.

Migration 0030 created live_gate_state, live_fills and trade_divergence
without the created_at/updated_at columns their models declare via
TimestampMixin, so every insert into those tables failed with
"table X has no column named created_at". That broke POST /trading/kill
(and would break live fills and divergence logging). All three tables are
empty in the live DB, so nullable columns are safe.

Revision ID: 0038_trading_timestamps
Revises: 0037_outcome_censoring
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_trading_timestamps"
down_revision = "0037_outcome_censoring"
branch_labels = None
depends_on = None

TABLES = ("live_gate_state", "live_fills", "trade_divergence")


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in TABLES:
        if table not in tables:
            continue
        columns = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        for col in ("created_at", "updated_at"):
            if col not in columns:
                op.add_column(table, sa.Column(col, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in TABLES:
        if table not in tables:
            continue
        columns = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        for col in ("created_at", "updated_at"):
            if col in columns:
                op.drop_column(table, col)
