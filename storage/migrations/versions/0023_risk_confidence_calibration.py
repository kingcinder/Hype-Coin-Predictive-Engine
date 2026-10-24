"""Persist versioned, time-validated confidence calibration maps."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_risk_confidence_calibration"
down_revision = "0022_solana_safety_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "risk_confidence_calibrations" not in tables:
        op.create_table(
            "risk_confidence_calibrations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("version", sa.String(64), nullable=False),
            sa.Column("status", sa.String(48), nullable=False),
            sa.Column("trained_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("training_started_at", sa.DateTime(timezone=True)),
            sa.Column("training_ended_at", sa.DateTime(timezone=True)),
            sa.Column("validation_started_at", sa.DateTime(timezone=True)),
            sa.Column("sample_size", sa.Integer(), nullable=False),
            sa.Column("validation_sample_size", sa.Integer(), nullable=False),
            sa.Column("baseline_ece", sa.Float()),
            sa.Column("calibrated_ece", sa.Float()),
            sa.Column("x_points", sa.JSON(), nullable=False),
            sa.Column("y_points", sa.JSON(), nullable=False),
            sa.Column("yellow_threshold", sa.Float()),
            sa.Column("orange_threshold", sa.Float()),
            sa.Column("red_threshold", sa.Float()),
            sa.Column("active", sa.Boolean(), nullable=False),
        )
        op.create_index(
            "ix_risk_confidence_calibrations_active",
            "risk_confidence_calibrations",
            ["active", "trained_at"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "risk_confidence_calibrations" in tables:
        op.drop_index(
            "ix_risk_confidence_calibrations_active",
            table_name="risk_confidence_calibrations",
        )
        op.drop_table("risk_confidence_calibrations")
