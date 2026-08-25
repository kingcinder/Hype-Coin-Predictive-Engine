"""Add alert snooze expiry."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_alert_snooze"
down_revision = "0014_alert_quality_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "alerts" not in tables:
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("alerts")}
    if "snoozed_until" not in columns:
        op.add_column(
            "alerts", sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "alerts" not in tables:
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("alerts")}
    if "snoozed_until" in columns:
        op.drop_column("alerts", "snoozed_until")
