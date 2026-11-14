"""Trading execution, live gate state, divergence and copy logs.

Renumbered from the prompt's ``0029_trading_execution`` because the B3 fix
occupies ``0029_relabel_support`` (see STATE.md).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_trading_execution"
down_revision = "0029_relabel_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "live_gate_state" not in tables:
        op.create_table(
            "live_gate_state",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("reason", sa.String(128), nullable=True),
        )
        op.create_index("ix_live_gate_state_kind_ts", "live_gate_state", ["kind", "ts"])

    if "live_fills" not in tables:
        op.create_table(
            "live_fills",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("order_id", sa.String(128), nullable=False),
            sa.Column("mint", sa.String(64), nullable=False),
            sa.Column("chain", sa.String(16), nullable=False),
            sa.Column("side", sa.String(8), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("price", sa.Numeric(38, 18), nullable=False),
            sa.Column("size_sol", sa.Numeric(30, 9), nullable=False),
            sa.Column("fee_sol", sa.Numeric(30, 9), nullable=False),
            sa.Column("slippage_pct", sa.Numeric(10, 4), nullable=False),
            sa.Column("tx_signature", sa.String(128), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("ledger_ref", sa.String(64), nullable=True),
        )

    if "trade_divergence" not in tables:
        op.create_table(
            "trade_divergence",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("trade_ref", sa.String(64), nullable=False),
            sa.Column("expected_price", sa.Numeric(38, 18), nullable=False),
            sa.Column("fill_price", sa.Numeric(38, 18), nullable=False),
            sa.Column("delta_pct", sa.Numeric(12, 4), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        )

    if "copy_log" not in tables:
        op.create_table(
            "copy_log",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("wallet", sa.String(64), nullable=False),
            sa.Column("mint", sa.String(64), nullable=False),
            sa.Column("size_sol", sa.Numeric(30, 9), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("detail", sa.Text, nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("copy_log", "trade_divergence", "live_fills"):
        if table in tables:
            op.drop_table(table)
    if "live_gate_state" in tables:
        indexes = {i["name"] for i in inspector.get_indexes("live_gate_state")}
        if "ix_live_gate_state_kind_ts" in indexes:
            op.drop_index("ix_live_gate_state_kind_ts", table_name="live_gate_state")
        op.drop_table("live_gate_state")
