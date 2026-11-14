"""Track when a copy approval claimed its log.

Revision ID: 0033_copy_log_claimed_at
Revises: 0032_ledger_trade_required_fields
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_copy_log_claimed_at"
down_revision = "0032_ledger_trade_required_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("copy_log") as batch_op:
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("copy_log") as batch_op:
        batch_op.drop_column("claimed_at")
