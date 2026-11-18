"""Add risk_outcomes.censored_at for unlabelable observation windows.

A due outcome with no collapse/survival lifecycle event inside its window can
never be labelled by the bounded evaluator.  Recording that censoring at window
close keeps the row auditable without retrying it as an unresolved backlog on
every scan.

Revision ID: 0037_outcome_censoring
Revises: 0036_alert_lookup_indexes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_outcome_censoring"
down_revision = "0036_alert_lookup_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "risk_outcomes" not in set(sa.inspect(bind).get_table_names()):
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("risk_outcomes")}
    if "censored_at" not in columns:
        op.add_column(
            "risk_outcomes",
            sa.Column("censored_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "risk_outcomes" not in set(sa.inspect(bind).get_table_names()):
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("risk_outcomes")}
    if "censored_at" in columns:
        op.drop_column("risk_outcomes", "censored_at")
