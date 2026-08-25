"""Risk outcome tracking and adaptive calibration

Revision ID: 0016_risk_outcomes
Revises: 0015_alert_snooze
Create Date: 2026-08-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_risk_outcomes"
down_revision = "0015_alert_snooze"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column(
            "score_id", sa.Integer(), sa.ForeignKey("scores.id"), nullable=False, unique=True
        ),
        sa.Column("risk_band", sa.String(16), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifecycle_phase_at_score", sa.String(32), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifecycle_phase_at_eval", sa.String(32), nullable=True),
        sa.Column("price_change_pct", sa.Float(), nullable=True),
        sa.Column("collapsed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("rugged", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("survived", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_risk_outcomes_score_id", "risk_outcomes", ["score_id"])
    op.create_index("ix_risk_outcomes_risk_band", "risk_outcomes", ["risk_band"])
    op.create_index("ix_risk_outcomes_scored_at", "risk_outcomes", ["scored_at"])

    op.create_table(
        "risk_calibrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("calibrated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("yellow_threshold", sa.Float(), nullable=False),
        sa.Column("orange_threshold", sa.Float(), nullable=False),
        sa.Column("red_threshold", sa.Float(), nullable=False),
        sa.Column("reason_weights", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("band_precisions", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_risk_calibrations_version", "risk_calibrations", ["version"])


def downgrade() -> None:
    op.drop_index("ix_risk_calibrations_version", table_name="risk_calibrations")
    op.drop_table("risk_calibrations")
    op.drop_index("ix_risk_outcomes_scored_at", table_name="risk_outcomes")
    op.drop_index("ix_risk_outcomes_risk_band", table_name="risk_outcomes")
    op.drop_index("ix_risk_outcomes_score_id", table_name="risk_outcomes")
    op.drop_table("risk_outcomes")
