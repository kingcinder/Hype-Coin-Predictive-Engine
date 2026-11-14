"""Add immutable prediction_records snapshots for out-of-sample evaluation."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_prediction_records"
down_revision = "0026_outcome_label_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("prediction_records"):
        return
    op.create_table(
        "prediction_records",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("risk_band", sa.String(16), nullable=False),
        sa.Column("risk_score", sa.Float, nullable=False),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("confidence_source", sa.String(16), nullable=False),
        sa.Column("missing_features", sa.JSON, nullable=True),
        sa.Column("code_sha", sa.String(40), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("calibration_snapshot_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_prediction_records_asset_scored_at",
        "prediction_records",
        ["asset_id", "scored_at"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("prediction_records"):
        return
    index_names = {index["name"] for index in inspector.get_indexes("prediction_records")}
    if "ix_prediction_records_asset_scored_at" in index_names:
        op.drop_index("ix_prediction_records_asset_scored_at", table_name="prediction_records")
    op.drop_table("prediction_records")
