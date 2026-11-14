"""Require stop_pct and the why/lesson fields on closed ledger trades.

Revision ID: 0032_ledger_trade_required_fields
Revises: 0031_whale_telemetry
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_ledger_trade_required_fields"
down_revision = "0031_whale_telemetry"
branch_labels = None
depends_on = None

_TABLE = "ledger_trades"


def upgrade() -> None:
    # Older rows may predate the required-field contract; keep them valid
    # without inventing non-empty explanations.
    op.execute(sa.text(f"UPDATE {_TABLE} SET stop_pct = 0 WHERE stop_pct IS NULL"))
    op.execute(sa.text(f"UPDATE {_TABLE} SET why_entered = '' WHERE why_entered IS NULL"))
    op.execute(sa.text(f"UPDATE {_TABLE} SET why_exited = '' WHERE why_exited IS NULL"))
    op.execute(sa.text(f"UPDATE {_TABLE} SET lesson = '' WHERE lesson IS NULL"))

    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column("stop_pct", existing_type=sa.Numeric(10, 4), nullable=False)
        batch_op.alter_column("why_entered", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("why_exited", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("lesson", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column("stop_pct", existing_type=sa.Numeric(10, 4), nullable=True)
        batch_op.alter_column("why_entered", existing_type=sa.Text(), nullable=True)
        batch_op.alter_column("why_exited", existing_type=sa.Text(), nullable=True)
        batch_op.alter_column("lesson", existing_type=sa.Text(), nullable=True)
