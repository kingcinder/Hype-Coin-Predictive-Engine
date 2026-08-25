"""phase 1-3 schema

Adds prelaunch_candidates, forecasts, and narrative_clusters tables. Written
defensively: revision 0001 creates the full current metadata, so this migration
only adds what is missing on pre-existing databases.

Revision ID: 0003_phases_1_2_3
Revises: 0002_radar_fingerprint
Create Date: 2026-08-19 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_phases_1_2_3"
down_revision = "0002_radar_fingerprint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "prelaunch_candidates" not in tables:
        op.create_table(
            "prelaunch_candidates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("decision_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("priority_score", sa.Float(), nullable=False),
            sa.Column("drivers", sa.JSON(), nullable=False),
            sa.Column("model_version", sa.String(length=128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "asset_id", "decision_ts", "model_version", name="uq_prelaunch_asset_ts"
            ),
        )
        op.create_index(
            "ix_prelaunch_asset_ts", "prelaunch_candidates", ["asset_id", "decision_ts"]
        )

    if "forecasts" not in tables:
        op.create_table(
            "forecasts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("decision_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("p_ignition_24h", sa.Float(), nullable=False),
            sa.Column("p_collapse_24h", sa.Float(), nullable=False),
            sa.Column("expected_hours_to_collapse", sa.Float(), nullable=True),
            sa.Column("calibration_bucket", sa.String(length=32), nullable=True),
            sa.Column("calibrated", sa.Boolean(), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("model_version", sa.String(length=128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "asset_id", "decision_ts", "model_version", name="uq_forecast_asset_ts_version"
            ),
        )
        op.create_index("ix_forecast_asset_ts", "forecasts", ["asset_id", "decision_ts"])
        op.create_index("ix_forecast_collapse", "forecasts", ["decision_ts", "p_collapse_24h"])

    if "narrative_clusters" not in tables:
        op.create_table(
            "narrative_clusters",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cluster_key", sa.String(length=128), nullable=False),
            sa.Column("seed_topic", sa.String(length=256), nullable=False),
            sa.Column("mention_count", sa.Integer(), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("cluster_key", name="uq_narrative_cluster_key"),
        )
        op.create_index("ix_narrative_clusters_last_seen", "narrative_clusters", ["last_seen_at"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table, indexes in (
        ("narrative_clusters", ["ix_narrative_clusters_last_seen"]),
        ("forecasts", ["ix_forecast_collapse", "ix_forecast_asset_ts"]),
        ("prelaunch_candidates", ["ix_prelaunch_asset_ts"]),
    ):
        if table in tables:
            for index in indexes:
                op.drop_index(index, table_name=table)
            op.drop_table(table)
