"""Whale and smart-money telemetry tables (Phase F).

The prompt numbers this 0030, but 0030 is already ``0030_trading_execution``
after the B3 relabel migration was inserted; this chains from it instead.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_whale_telemetry"
down_revision = "0030_trading_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "first_buys" not in tables:
        op.create_table(
            "first_buys",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("wallet", sa.String(64), nullable=False),
            sa.Column("size_usd", sa.Numeric(30, 9), nullable=True),
            sa.Column("seconds_after_pair_creation", sa.Integer, nullable=True),
            sa.Column("is_tracked_wallet", sa.Boolean, nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_first_buys_asset_ts", "first_buys", ["asset_id", "ts"])

    if "pair_first_window_stats" not in tables:
        op.create_table(
            "pair_first_window_stats",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("window_minutes", sa.Integer, nullable=True),
            sa.Column("buys", sa.Integer, nullable=True),
            sa.Column("sells", sa.Integer, nullable=True),
            sa.Column("buy_sell_ratio", sa.Numeric(18, 6), nullable=True),
            sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("asset_id", name="uq_pair_first_window_asset"),
        )

    if "dev_wallet_stats" not in tables:
        op.create_table(
            "dev_wallet_stats",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("deployer", sa.String(64), nullable=True),
            sa.Column("dev_holding_pct", sa.Numeric(10, 4), nullable=True),
            sa.Column("dev_sold", sa.Boolean, nullable=True),
            sa.Column("last_checked_ts", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("asset_id", name="uq_dev_wallet_stats_asset"),
        )

    if "holder_metrics" not in tables:
        op.create_table(
            "holder_metrics",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("top10_pct", sa.Numeric(10, 4), nullable=True),
            sa.Column("holder_count", sa.Integer, nullable=True),
            sa.Column("holder_growth_per_hour", sa.Numeric(18, 6), nullable=True),
        )
        op.create_index("ix_holder_metrics_asset_ts", "holder_metrics", ["asset_id", "ts"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("holder_metrics", "dev_wallet_stats", "pair_first_window_stats", "first_buys"):
        if table in tables:
            op.drop_table(table)
