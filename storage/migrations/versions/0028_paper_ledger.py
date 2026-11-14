"""Create paper ledger tables for trade tracking."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_paper_ledger"
down_revision = "0027_prediction_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = {t for t in inspector.get_table_names()}

    # ledger_open_positions
    if "ledger_open_positions" not in tables:
        op.create_table(
            "ledger_open_positions",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("mode", sa.String(8), nullable=False),
            sa.Column("mint", sa.String(64), nullable=False),
            sa.Column("chain", sa.String(16), nullable=False),
            sa.Column("source_signal", sa.String(128), nullable=False),
            sa.Column("entry_quote_price", sa.Numeric(38, 18), nullable=False),
            sa.Column("entry_effective_price", sa.Numeric(38, 18), nullable=False),
            sa.Column("qty", sa.Numeric(38, 12), nullable=False),
            sa.Column("size_sol", sa.Numeric(30, 9), nullable=False),
            sa.Column("entry_fees_sol", sa.Numeric(30, 9), nullable=False),
            sa.Column("entry_slip_pct", sa.Numeric(10, 4), nullable=False),
            sa.Column("is_new_token", sa.Boolean, nullable=False),
            sa.Column("stop_pct", sa.Numeric(10, 4), nullable=False),
            sa.Column("target_pct", sa.Numeric(10, 4), nullable=True),
            sa.Column("time_stop_minutes", sa.Integer, nullable=True),
            sa.Column("stop_price", sa.Numeric(38, 18), nullable=False),
            sa.Column("target_price", sa.Numeric(38, 18), nullable=True),
            sa.Column("time_stop_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("price_source", sa.String(32), nullable=False),
            sa.Column("price_source_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("candidate_evidence_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("why_entered", sa.Text, nullable=False),
            sa.Column("rules_sha256", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    # ledger_trades
    if "ledger_trades" not in tables:
        op.create_table(
            "ledger_trades",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("mode", sa.String(8), nullable=False),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("mint", sa.String(64), nullable=False),
            sa.Column("chain", sa.String(16), nullable=False),
            sa.Column("source_signal", sa.String(128), nullable=False),
            sa.Column("entry_price", sa.Numeric(38, 18), nullable=False),
            sa.Column("size_sol", sa.Numeric(30, 9), nullable=False),
            sa.Column("fees_sol", sa.Numeric(30, 9), nullable=False),
            sa.Column("slippage_sol", sa.Numeric(30, 9), nullable=False),
            sa.Column("stop_pct", sa.Numeric(10, 4), nullable=True),
            sa.Column("target_pct", sa.Numeric(10, 4), nullable=True),
            sa.Column("time_stop_minutes", sa.Integer, nullable=True),
            sa.Column("exit_price", sa.Numeric(38, 18), nullable=False),
            sa.Column("pnl_sol", sa.Numeric(30, 9), nullable=False),
            sa.Column("tx_signature", sa.String(128), nullable=True),
            sa.Column("why_entered", sa.Text, nullable=True),
            sa.Column("why_exited", sa.Text, nullable=True),
            sa.Column("lesson", sa.Text, nullable=True),
            sa.Column("candidate_evidence_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("price_source", sa.String(32), nullable=False),
            sa.Column("price_source_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("rules_sha256", sa.String(64), nullable=False),
            sa.Column(
                "correction_of",
                sa.Integer,
                sa.ForeignKey("ledger_trades.id"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    # ledger_review_acks
    if "ledger_review_acks" not in tables:
        op.create_table(
            "ledger_review_acks",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("acked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("note", sa.Text, nullable=True),
        )

    # ledger_scorecards
    if "ledger_scorecards" not in tables:
        op.create_table(
            "ledger_scorecards",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("mode", sa.String(8), nullable=True),
            sa.Column("payload", sa.JSON, nullable=False),
        )

    # price_evidence
    if "price_evidence" not in tables:
        op.create_table(
            "price_evidence",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("mint", sa.String(64), nullable=False),
            sa.Column("chain", sa.String(16), nullable=False),
            sa.Column("side", sa.String(8), nullable=False),
            sa.Column("price", sa.Numeric(38, 18), nullable=False),
            sa.Column("price_source", sa.String(32), nullable=False),
            sa.Column("source_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("trade_ref", sa.String(64), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = {t for t in inspector.get_table_names()}

    for table in (
        "price_evidence",
        "ledger_scorecards",
        "ledger_review_acks",
        "ledger_trades",
        "ledger_open_positions",
    ):
        if table in tables:
            op.drop_table(table)
