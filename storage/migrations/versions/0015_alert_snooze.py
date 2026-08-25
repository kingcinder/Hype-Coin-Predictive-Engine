"""Add alert snooze expiry."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_alert_snooze"
down_revision = "0014_alert_quality_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("alerts", "snoozed_until")
