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


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
