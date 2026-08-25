"""Add ensemble_fed_at column to risk_outcomes for per-token feedback tracking.

Revision ID: 0017
Revises: 0016_risk_outcomes
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016_risk_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "risk_outcomes" not in tables:
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("risk_outcomes")}
    if "ensemble_fed_at" not in columns:
        op.add_column(
            "risk_outcomes",
            sa.Column(
                "ensemble_fed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "risk_outcomes" not in tables:
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("risk_outcomes")}
    if "ensemble_fed_at" in columns:
        op.drop_column("risk_outcomes", "ensemble_fed_at")
