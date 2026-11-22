"""Add entry_price_native to ledger_open_positions.

The paper engine sized positions as qty = size_sol / price_usd, which is
SOL/USD too small (BANDANA CAT recorded 286,576,168 tokens vs ~19.67e9
real tokens at SOL $68.64). With the native price stored at entry, qty is
a real token count and closes price in native currency; positions opened
before the fix have NULL here and keep closing on the USD ratio, so the
one open legacy position (BANDANA CAT) is never closed on mixed units.

Nullable column: the live table has exactly one open row and no closed
rows read this column. Downgrade drops it.

Revision ID: 0040_ledger_entry_price_native
Revises: 0039_feature_observed_at_evidence_time
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_ledger_entry_price_native"
down_revision = "0039_feature_observed_at_evidence_time"
branch_labels = None
depends_on = None

TABLE = "ledger_open_positions"
COLUMN = "entry_price_native"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}
    if COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.Numeric(38, 18), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}
    if COLUMN in columns:
        op.drop_column(TABLE, COLUMN)
