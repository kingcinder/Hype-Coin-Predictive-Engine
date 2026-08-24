"""alert push notification column

Adds ``notified_at`` to alerts so the ntfy.sh notifier can mark pushed rows
idempotently. Written defensively: revision 0001 creates the full current
metadata, so this migration only adds the column on pre-existing databases.

Revision ID: 0004_alert_notification
Revises: 0003_phases_1_2_3
Create Date: 2026-08-19 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_alert_notification"
down_revision = "0003_phases_1_2_3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "alerts" not in tables:
        return
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("alerts")}
    if "notified_at" not in columns:
        op.add_column(
            "alerts",
            sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "alerts" not in tables:
        return
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("alerts")}
    if "notified_at" in columns:
        op.drop_column("alerts", "notified_at")
