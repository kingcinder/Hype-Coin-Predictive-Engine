"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-01 00:00:00
"""

from __future__ import annotations

from alembic import op

from storage import models  # noqa: F401 registers metadata
from storage.database import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# Tables created by LATER migrations (0002+). ``Base.metadata.create_all``
# reflects the CURRENT full schema, so 0001 must create only the base tables
# it owns — otherwise a fresh ``alembic upgrade head`` creates every table in
# 0001 and then each later migration collides with "table X already exists"
# (the live 0011_scan_results / 0013_parity_mismatches failures). When adding
# a new migration that creates a table, add that table to this set too.
_LATER_OWNED_TABLES: frozenset[str] = frozenset(
    {
        # 0002_radar_fingerprint
        "fingerprint_assessments",
        "ignition_events",
        # 0003_phases_1_2_3
        "forecasts",
        "narrative_clusters",
        "prelaunch_candidates",
        # 0005_archive
        "archive_manifests",
        # 0006_pump_physics
        "lifecycle_events",
        # 0007_retention
        "retention_runs",
        # 0008_rpc_pool_snapshots
        "rpc_pool_snapshots",
        # 0009_notification_digest
        "notification_digests",
        # 0010_liquidity_removal_events
        "liquidity_removal_events",
        # 0011_scan_results
        "scan_results",
        # 0013_parity_mismatches
        "parity_mismatches",
        # 0014_alert_quality_controls
        "alert_type_controls",
        # 0016_risk_outcomes
        "risk_outcomes",
        "risk_calibrations",
        # 0018_ensemble_state_and_cross_source
        "ensemble_state",
        "cross_source_signals",
    }
)


def _base_tables() -> list:
    """Only the tables 0001 owns — everything later migrations create is
    excluded so the migration chain builds cleanly on a fresh database."""
    return [table for table in Base.metadata.sorted_tables if table.name not in _LATER_OWNED_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=_base_tables())


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, tables=_base_tables())
