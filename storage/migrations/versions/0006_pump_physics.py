"""pump_physics lifecycle schema

Adds the ``lifecycle_events`` table (hype-lifecycle state machine
transitions) and ``forecasts.expected_hours_to_peak`` (the Phase-3 artifact's
time-to-peak estimate). Written defensively: revision 0001 creates the full
current metadata, so this migration only adds the delta on pre-existing
databases.

Revision ID: 0006_pump_physics
Revises: 0005_archive
Create Date: 2026-08-19 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_pump_physics"
down_revision = "0005_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "lifecycle_events" not in tables:
        op.create_table(
            "lifecycle_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("phase", sa.String(length=32), nullable=False),
            sa.Column(
                "event_type",
                sa.String(length=32),
                server_default="phase_transition",
                nullable=False,
            ),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("confidence", sa.Float(), server_default="0.5", nullable=False),
            sa.Column("details", sa.JSON(), server_default="{}", nullable=False),
            sa.UniqueConstraint(
                "asset_id", "phase", "ts", "event_type", name="uq_lifecycle_asset_phase_ts"
            ),
        )
        op.create_index("ix_lifecycle_asset_ts", "lifecycle_events", ["asset_id", "ts"])

    if "forecasts" in tables:
        columns = {column["name"] for column in inspector.get_columns("forecasts")}
        if "expected_hours_to_peak" not in columns:
            op.add_column(
                "forecasts",
                sa.Column("expected_hours_to_peak", sa.Float(), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "forecasts" in tables:
        columns = {column["name"] for column in inspector.get_columns("forecasts")}
        if "expected_hours_to_peak" in columns:
            op.drop_column("forecasts", "expected_hours_to_peak")

    if "lifecycle_events" in tables:
        op.drop_table("lifecycle_events")
