"""Add alert lookup indexes used by scoring.

Revision ID: 0036_alert_lookup_indexes
Revises: 0035_rpc_endpoint_success_counts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_alert_lookup_indexes"
down_revision = "0035_rpc_endpoint_success_counts"
branch_labels = None
depends_on = None

_INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ix_alerts_asset_score_type", ("asset_id", "score_id", "alert_type")),
    ("ix_alerts_type_acked", ("alert_type", "acked_at")),
)


def upgrade() -> None:
    bind = op.get_bind()
    if "alerts" not in set(sa.inspect(bind).get_table_names()):
        return
    existing = {index["name"] for index in sa.inspect(bind).get_indexes("alerts")}
    for name, columns in _INDEXES:
        if name not in existing:
            op.create_index(name, "alerts", list(columns))


def downgrade() -> None:
    bind = op.get_bind()
    if "alerts" not in set(sa.inspect(bind).get_table_names()):
        return
    existing = {index["name"] for index in sa.inspect(bind).get_indexes("alerts")}
    for name, _columns in _INDEXES:
        if name in existing:
            op.drop_index(name, table_name="alerts")
