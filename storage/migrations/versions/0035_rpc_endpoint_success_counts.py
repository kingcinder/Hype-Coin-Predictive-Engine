"""Track live RPC successes per endpoint in pool snapshots.

Revision ID: 0035_rpc_endpoint_success_counts
Revises: 0034_rpc_endpoint_request_counts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_rpc_endpoint_success_counts"
down_revision = "0034_rpc_endpoint_request_counts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rpc_pool_snapshots") as batch_op:
        batch_op.add_column(
            sa.Column("success_count", sa.Integer(), server_default="0", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("rpc_pool_snapshots") as batch_op:
        batch_op.drop_column("success_count")
