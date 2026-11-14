"""Add risk_outcomes.label_version so bounded labels are distinguishable."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_outcome_label_version"
down_revision = "0025_score_confidence_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("risk_outcomes")}
    if "label_version" not in cols:
        op.add_column(
            "risk_outcomes",
            sa.Column(
                "label_version", sa.String(16), nullable=False, server_default="v1_unbounded"
            ),
        )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("risk_outcomes")}
    if "label_version" in cols:
        op.drop_column("risk_outcomes", "label_version")
