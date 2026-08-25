"""radar and fingerprint schema

Adds ignition_events and fingerprint_assessments tables, plus a role column on
wallet_cluster_members. Revision 0001 creates the full current metadata, so this
migration is written defensively: it only adds what is missing on pre-existing
databases.

Revision ID: 0002_radar_fingerprint
Revises: 0001_initial
Create Date: 2026-08-19 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_radar_fingerprint"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()

    if "wallet_cluster_members" in tables:
        inspector = sa.inspect(op.get_bind())
        columns = {column["name"] for column in inspector.get_columns("wallet_cluster_members")}
        if "role" not in columns:
            op.add_column(
                "wallet_cluster_members",
                sa.Column("role", sa.String(length=32), nullable=False, server_default="unknown"),
            )

    if "ignition_events" not in tables:
        op.create_table(
            "ignition_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
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
                "asset_id", "event_type", "ts", "source_id", name="uq_ignition_event"
            ),
        )
        op.create_index("ix_ignition_events_asset_ts", "ignition_events", ["asset_id", "ts"])
        op.create_index("ix_ignition_events_type_ts", "ignition_events", ["event_type", "ts"])

    if "fingerprint_assessments" not in tables:
        op.create_table(
            "fingerprint_assessments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("decision_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("recidivism_score", sa.Float(), nullable=False),
            sa.Column("matched_cluster_count", sa.Integer(), nullable=False),
            sa.Column("matched_wallet_count", sa.Integer(), nullable=False),
            sa.Column("matched_roles", sa.JSON(), nullable=False),
            sa.Column("matched_clusters", sa.JSON(), nullable=False),
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
                "asset_id",
                "decision_ts",
                "model_version",
                name="uq_fingerprint_asset_ts_version",
            ),
        )
        op.create_index(
            "ix_fingerprint_asset_ts", "fingerprint_assessments", ["asset_id", "decision_ts"]
        )


def downgrade() -> None:
    tables = _tables()
    if "fingerprint_assessments" in tables:
        op.drop_index("ix_fingerprint_asset_ts", table_name="fingerprint_assessments")
        op.drop_table("fingerprint_assessments")
    if "ignition_events" in tables:
        op.drop_index("ix_ignition_events_type_ts", table_name="ignition_events")
        op.drop_index("ix_ignition_events_asset_ts", table_name="ignition_events")
        op.drop_table("ignition_events")
    if "wallet_cluster_members" in tables:
        inspector = sa.inspect(op.get_bind())
        columns = {column["name"] for column in inspector.get_columns("wallet_cluster_members")}
        if "role" in columns:
            op.drop_column("wallet_cluster_members", "role")
