"""archive schema: Parquet compaction support

Adds the ``archive_manifests`` table, an ``archived_at`` column on raw
evidence items, and an ``observed_at`` index for the compactor's scan.
Written defensively: revision 0001 creates the full current metadata, so
this migration only adds the delta on pre-existing databases.

Revision ID: 0005_archive
Revises: 0004_alert_notification
Create Date: 2026-08-19 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_archive"
down_revision = "0004_alert_notification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "archive_manifests" not in tables:
        op.create_table(
            "archive_manifests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("object_key", sa.String(length=1024), nullable=False),
            sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
            sa.Column("partition_year", sa.Integer(), nullable=False),
            sa.Column("partition_month", sa.Integer(), nullable=False),
            sa.Column("row_count", sa.Integer(), nullable=False),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("object_key", name="uq_archive_manifest_object_key"),
        )

    if "raw_evidence_items" in tables:
        columns = {column["name"] for column in inspector.get_columns("raw_evidence_items")}
        if "archived_at" not in columns:
            op.add_column(
                "raw_evidence_items",
                sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            )
        indexes = {index["name"] for index in inspector.get_indexes("raw_evidence_items")}
        if "ix_raw_evidence_observed_at" not in indexes:
            op.create_index("ix_raw_evidence_observed_at", "raw_evidence_items", ["observed_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "raw_evidence_items" in tables:
        indexes = {index["name"] for index in inspector.get_indexes("raw_evidence_items")}
        if "ix_raw_evidence_observed_at" in indexes:
            op.drop_index("ix_raw_evidence_observed_at", table_name="raw_evidence_items")
        columns = {column["name"] for column in inspector.get_columns("raw_evidence_items")}
        if "archived_at" in columns:
            op.drop_column("raw_evidence_items", "archived_at")

    if "archive_manifests" in tables:
        op.drop_table("archive_manifests")
