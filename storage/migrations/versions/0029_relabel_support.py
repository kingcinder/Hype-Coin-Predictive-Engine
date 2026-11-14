"""Allow bounded relabel copies: drop per-score uniqueness, add correction_of.

B3's relabel is append-only: a bounded (v2) label is a NEW ``risk_outcomes``
row that points at the row it corrects. That needs two rows per score, so the
column-level UNIQUE on ``score_id`` must relax to
``UNIQUE (score_id, label_version)``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_relabel_support"
down_revision = "0028_paper_ledger"
branch_labels = None
depends_on = None

_TABLE = "risk_outcomes"
_TEMP = "risk_outcomes_relabel_tmp"
_INDEXES = (
    ("ix_risk_outcomes_score_id", ["score_id"]),
    ("ix_risk_outcomes_risk_band", ["risk_band"]),
    ("ix_risk_outcomes_scored_at", ["scored_at"]),
)
_COPY_COLUMNS = (
    "id",
    "asset_id",
    "score_id",
    "risk_band",
    "scored_at",
    "lifecycle_phase_at_score",
    "evaluated_at",
    "lifecycle_phase_at_eval",
    "price_change_pct",
    "collapsed",
    "rugged",
    "survived",
    "ensemble_fed_at",
    "details",
    "created_at",
    "label_version",
)


def _columns(*, bounded: bool) -> list[sa.Column]:
    columns: list[sa.Column] = [
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id"), nullable=False),
        sa.Column(
            "score_id",
            sa.Integer,
            sa.ForeignKey("scores.id"),
            nullable=False,
            unique=not bounded,
        ),
        sa.Column("risk_band", sa.String(16), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifecycle_phase_at_score", sa.String(32), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifecycle_phase_at_eval", sa.String(32), nullable=True),
        sa.Column("price_change_pct", sa.Float, nullable=True),
        sa.Column("collapsed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("rugged", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("survived", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("ensemble_fed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "label_version",
            sa.String(16),
            nullable=False,
            server_default="v1_unbounded",
        ),
    ]
    if bounded:
        columns.append(sa.Column("correction_of", sa.Integer, nullable=True))
    return columns


def _swap(*, bounded: bool) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    existing = {column["name"] for column in inspector.get_columns(_TABLE)}
    if bounded and "correction_of" in existing:
        return
    if not bounded and "correction_of" not in existing:
        return

    meta = sa.MetaData()
    meta.reflect(bind=bind, only=["assets", "scores", _TABLE])
    old = meta.tables[_TABLE]

    temp = sa.Table(_TEMP, meta, *_columns(bounded=bounded))
    if bounded:
        temp.append_constraint(
            sa.UniqueConstraint("score_id", "label_version", name="uq_risk_outcomes_score_label")
        )
    temp.create(bind)

    names = [name for name in _COPY_COLUMNS if name in existing]
    bind.execute(temp.insert().from_select(names, sa.select(*[old.c[name] for name in names])))
    op.drop_table(_TABLE)
    op.rename_table(_TEMP, _TABLE)
    for index_name, columns in _INDEXES:
        op.create_index(index_name, _TABLE, columns)


def upgrade() -> None:
    _swap(bounded=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        columns = {column["name"] for column in inspector.get_columns(_TABLE)}
        if "correction_of" in columns:
            # Copies must go before per-score uniqueness can be restored.
            bind.execute(sa.text(f"DELETE FROM {_TABLE} WHERE label_version != 'v1_unbounded'"))
    _swap(bounded=False)
