"""Add scores.confidence_source so fallback confidence is stamped, not implied."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_score_confidence_source"
down_revision = "0024_independent_market_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("scores")}
    if "confidence_source" not in columns:
        op.add_column(
            "scores",
            sa.Column(
                "confidence_source", sa.String(16), nullable=False, server_default="fallback"
            ),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("scores")}
    if "confidence_source" in columns:
        op.drop_column("scores", "confidence_source")
