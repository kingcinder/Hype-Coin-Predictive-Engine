"""alert acknowledgement columns

Adds ``acked_at`` and ``ack_quality`` to alerts so operators can ACK open
alerts: an ACKed alert leaves the notifier's open set (suppressing repeat
pushes — the notifier only pushes ``state=open`` rows) and records whether
the operator found the alert useful or noise, feeding the signal-quality
ledger. Written defensively like 0004: revision 0001 creates the full current
metadata, so this migration only adds the columns on pre-existing databases.

Revision ID: 0012_alert_ack
Revises: 0011_scan_results
Create Date: 2026-08-24 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_alert_ack"
down_revision = "0011_scan_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "alerts" not in tables:
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("alerts")}
    if "acked_at" not in columns:
        op.add_column(
            "alerts",
            sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "ack_quality" not in columns:
        op.add_column(
            "alerts",
            sa.Column("ack_quality", sa.String(32), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "alerts" not in tables:
        return
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("alerts")}
    if "ack_quality" in columns:
        op.drop_column("alerts", "ack_quality")
    if "acked_at" in columns:
        op.drop_column("alerts", "acked_at")
