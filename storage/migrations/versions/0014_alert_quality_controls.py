"""Add alert quality quieting controls."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_alert_quality_controls"
down_revision = "0013_parity_mismatches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "alert_type_controls" not in tables:
        op.create_table(
            "alert_type_controls",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("alert_type", sa.String(128), nullable=False, unique=True),
            sa.Column("reenabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "alert_type_controls" in tables:
        op.drop_table("alert_type_controls")
