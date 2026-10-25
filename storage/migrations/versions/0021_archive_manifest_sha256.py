"""archive_manifest_sha256: content hash of the stored Parquet object.

Adds ``archive_manifests.sha256`` (nullable): the SHA-256 hex of the exact
bytes the compactor wrote for the partition, captured at write time. The
compactor fills it on every merge; rows written before this revision keep
NULL. Lets operators verify a stored object against the manifest instead of
discovering bit-rot only on the next read (after the DB originals were
already pruned).

Revision ID: 0021_archive_manifest_sha256
Revises: 0020_score_drift_runs
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_archive_manifest_sha256"
down_revision = "0020_score_drift_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "archive_manifests" not in tables:
        return
    columns = {col["name"] for col in sa.inspect(bind).get_columns("archive_manifests")}
    if "sha256" not in columns:
        op.add_column("archive_manifests", sa.Column("sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "archive_manifests" not in tables:
        return
    columns = {col["name"] for col in sa.inspect(bind).get_columns("archive_manifests")}
    if "sha256" in columns:
        op.drop_column("archive_manifests", "sha256")
