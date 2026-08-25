"""ML-specific probability thresholds for risk calibration

Revision ID: 0019_ml_probability_thresholds
Revises: 0018
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_ml_probability_thresholds"
down_revision = "0018"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "risk_outcomes" in tables:
        # RiskOutcome gains a details JSON snapshot holding the ML-specific
        # prediction (ml_risk_band / ml_prediction) so the ML scorer can be
        # calibrated independently of the rule engine.
        if "details" not in _columns("risk_outcomes"):
            op.add_column(
                "risk_outcomes",
                sa.Column(
                    "details",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                ),
            )
    if "risk_calibrations" in tables:
        columns = _columns("risk_calibrations")
        if "ml_yellow_threshold" not in columns:
            op.add_column(
                "risk_calibrations",
                sa.Column(
                    "ml_yellow_threshold",
                    sa.Float(),
                    nullable=False,
                    server_default=sa.text("0.1"),
                ),
            )
        if "ml_orange_threshold" not in columns:
            op.add_column(
                "risk_calibrations",
                sa.Column(
                    "ml_orange_threshold",
                    sa.Float(),
                    nullable=False,
                    server_default=sa.text("0.3"),
                ),
            )
        if "ml_red_threshold" not in columns:
            op.add_column(
                "risk_calibrations",
                sa.Column(
                    "ml_red_threshold",
                    sa.Float(),
                    nullable=False,
                    server_default=sa.text("0.5"),
                ),
            )
        if "ml_band_precisions" not in columns:
            op.add_column(
                "risk_calibrations",
                sa.Column(
                    "ml_band_precisions",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                ),
            )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "risk_calibrations" in tables:
        columns = _columns("risk_calibrations")
        if "ml_band_precisions" in columns:
            op.drop_column("risk_calibrations", "ml_band_precisions")
        if "ml_red_threshold" in columns:
            op.drop_column("risk_calibrations", "ml_red_threshold")
        if "ml_orange_threshold" in columns:
            op.drop_column("risk_calibrations", "ml_orange_threshold")
        if "ml_yellow_threshold" in columns:
            op.drop_column("risk_calibrations", "ml_yellow_threshold")
    if "risk_outcomes" in tables:
        if "details" in _columns("risk_outcomes"):
            op.drop_column("risk_outcomes", "details")
