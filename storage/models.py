from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from common.time import utc_now
from storage.database import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Chain(Base, TimestampMixin):
    __tablename__ = "chains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    vm_type: Mapped[str] = mapped_column(String(32), nullable=False)
    native_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    finality_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Source(Base, TimestampMixin):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Asset(Base, TimestampMixin):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("chain_id", "address", name="uq_assets_chain_address"),
        Index("ix_assets_chain_symbol", "chain_id", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), nullable=False)
    address: Mapped[str] = mapped_column(String(160), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(256))
    asset_type: Mapped[str] = mapped_column(String(64), default="token", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="raw_discovery", nullable=False)
    identity_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    website_url: Mapped[str | None] = mapped_column(String(1024))
    github_url: Mapped[str | None] = mapped_column(String(1024))


class Contract(Base, TimestampMixin):
    __tablename__ = "contracts"
    __table_args__ = (UniqueConstraint("chain_id", "address", name="uq_contracts_chain_address"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), nullable=False)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"))
    address: Mapped[str] = mapped_column(String(160), nullable=False)
    verified_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    proxy_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    implementation_address: Mapped[str | None] = mapped_column(String(160))
    deployer_wallet: Mapped[str | None] = mapped_column(String(160))
    bytecode_hash: Mapped[str | None] = mapped_column(String(128))
    abi_hash: Mapped[str | None] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Venue(Base, TimestampMixin):
    __tablename__ = "venues"
    __table_args__ = (UniqueConstraint("name", "chain_id", name="uq_venues_name_chain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    chain_id: Mapped[int | None] = mapped_column(ForeignKey("chains.id"))
    official_url: Mapped[str | None] = mapped_column(String(512))


class Pool(Base, TimestampMixin):
    __tablename__ = "pools"
    __table_args__ = (UniqueConstraint("chain_id", "address", name="uq_pools_chain_address"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), nullable=False)
    address: Mapped[str] = mapped_column(String(192), nullable=False)
    dex_id: Mapped[str] = mapped_column(String(128), nullable=False)
    base_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    quote_asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"))
    created_at_source: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Pair(Base, TimestampMixin):
    __tablename__ = "pairs"
    __table_args__ = (
        UniqueConstraint(
            "venue_id", "base_asset_id", "quote_asset_id", "pool_id", name="uq_pairs_natural"
        ),
        Index("ix_pairs_base_asset", "base_asset_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"), nullable=False)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), nullable=False)
    base_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    quote_asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"))
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("pools.id"))
    created_at_source: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionRun(Base):
    """One retention-autopilot pass: lake totals and growth vs the previous run.

    The autopilot runs compaction + pruning on a configurable cadence and
    records the Parquet lake's size here so Feed Health can report how fast the
    evidence lake is growing (``growth_bytes`` / ``growth_pct`` since the
    previous retention pass)."""

    __tablename__ = "retention_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    partitions: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    compacted: Mapped[int] = mapped_column(Integer, nullable=False)
    pruned: Mapped[int] = mapped_column(Integer, nullable=False)
    growth_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    growth_pct: Mapped[float | None] = mapped_column(Float)
    duration_sec: Mapped[float | None] = mapped_column(Float)


class RawEvidenceItem(Base):
    __tablename__ = "raw_evidence_items"
    __table_args__ = (
        UniqueConstraint("source_id", "content_hash", name="uq_raw_evidence_source_hash"),
        Index("ix_raw_evidence_observed_at", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_tier: Mapped[str] = mapped_column(String(64), nullable=False)
    url_hash: Mapped[str | None] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    raw_path: Mapped[str | None] = mapped_column(String(1024))
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchiveManifest(Base):
    """One row per Parquet partition written by the ops/archive compactor."""

    __tablename__ = "archive_manifests"
    __table_args__ = (
        UniqueConstraint("object_key", name="uq_archive_manifest_object_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    partition_year: Mapped[int] = mapped_column(Integer, nullable=False)
    partition_month: Mapped[int] = mapped_column(Integer, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint("pair_id", "ts", "source_id", name="uq_market_pair_ts_source"),
        Index("ix_market_pair_ts", "pair_id", "ts"),
        Index("ix_market_observed_at", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("pairs.id"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    price_usd: Mapped[float | None] = mapped_column(Float)
    volume_usd: Mapped[float | None] = mapped_column(Float)
    buys: Mapped[int | None] = mapped_column(Integer)
    sells: Mapped[int | None] = mapped_column(Integer)
    trades: Mapped[int | None] = mapped_column(Integer)
    raw_evidence_id: Mapped[int | None] = mapped_column(ForeignKey("raw_evidence_items.id"))


class LiquiditySnapshot(Base):
    __tablename__ = "liquidity_snapshots"
    __table_args__ = (
        UniqueConstraint("pool_id", "ts", "source_id", name="uq_liquidity_pool_ts_source"),
        Index("ix_liquidity_pool_ts", "pool_id", "ts"),
        Index("ix_liquidity_observed_at", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reserve_base: Mapped[float | None] = mapped_column(Float)
    reserve_quote: Mapped[float | None] = mapped_column(Float)
    reserve_usd: Mapped[float | None] = mapped_column(Float)
    lp_concentration_hhi: Mapped[float | None] = mapped_column(Float)
    raw_evidence_id: Mapped[int | None] = mapped_column(ForeignKey("raw_evidence_items.id"))


class Holder(Base):
    __tablename__ = "holders"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "wallet_address", "ts", "source_id", name="uq_holders_asset_wallet_ts"
        ),
        Index("ix_holders_asset_ts", "asset_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    wallet_address: Mapped[str] = mapped_column(String(192), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    pct_supply: Mapped[float | None] = mapped_column(Float)


class WalletCluster(Base, TimestampMixin):
    __tablename__ = "wallet_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    method_version: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WalletClusterMember(Base):
    __tablename__ = "wallet_cluster_members"
    __table_args__ = (
        UniqueConstraint("cluster_id", "wallet_address", name="uq_wallet_cluster_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("wallet_clusters.id"), nullable=False)
    wallet_address: Mapped[str] = mapped_column(String(192), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)


class ContractFlag(Base):
    __tablename__ = "contract_flags"
    __table_args__ = (
        UniqueConstraint("contract_id", "ts", "flag_type", "source_id", name="uq_contract_flag"),
        Index("ix_contract_flags_contract_ts", "contract_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    flag_type: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("raw_evidence_items.id"))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class SocialMention(Base):
    __tablename__ = "social_mentions"
    __table_args__ = (Index("ix_social_mentions_asset_ts", "asset_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"))
    topic: Mapped[str | None] = mapped_column(String(256))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    author_hash: Mapped[str | None] = mapped_column(String(128))
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(1024))


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (UniqueConstraint("url_hash", name="uq_news_url_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_domain: Mapped[str] = mapped_column(String(256), nullable=False)
    title_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    official_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_evidence_id: Mapped[int | None] = mapped_column(ForeignKey("raw_evidence_items.id"))


class Catalyst(Base, TimestampMixin):
    __tablename__ = "catalysts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    catalyst_type: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "decision_ts", "feature_name", name="uq_features_asset_ts_name"
        ),
        Index("ix_features_asset_decision", "asset_id", "decision_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    decision_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    feature_name: Mapped[str] = mapped_column(String(128), nullable=False)
    feature_value: Mapped[float] = mapped_column(Float, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    freshness_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    missing_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "decision_ts", "model_version", name="uq_scores_asset_ts_model"
        ),
        Index("ix_scores_research_priority", "decision_ts", "research_priority"),
        Index("ix_scores_risk_band", "risk_band"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    decision_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hype: Mapped[float] = mapped_column(Float, nullable=False)
    ethos: Mapped[float] = mapped_column(Float, nullable=False)
    risk: Mapped[float] = mapped_column(Float, nullable=False)
    liquidity_access: Mapped[float] = mapped_column(Float, nullable=False)
    manipulation: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    uncertainty: Mapped[float] = mapped_column(Float, nullable=False)
    catalyst: Mapped[float] = mapped_column(Float, nullable=False)
    exit_risk: Mapped[float] = mapped_column(Float, nullable=False)
    research_priority: Mapped[float] = mapped_column(Float, nullable=False)
    risk_band: Mapped[str] = mapped_column(String(16), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)


class ScoreExplanation(Base):
    __tablename__ = "score_explanations"
    __table_args__ = (UniqueConstraint("score_id", name="uq_score_explanations_score"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    score_id: Mapped[int] = mapped_column(ForeignKey("scores.id"), nullable=False)
    drivers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    risk_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    missing_features: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    changed_features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    score_id: Mapped[int | None] = mapped_column(ForeignKey("scores.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    alert_type: Mapped[str] = mapped_column(String(128), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(128), nullable=False)
    score_snapshot_ref: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationDigest(Base):
    """Durable once-per-UTC-day state for the ntfy event digest."""

    __tablename__ = "notification_digests"
    __table_args__ = (UniqueConstraint("digest_key", name="uq_notification_digest_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_key: Mapped[str] = mapped_column(String(32), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ignition_count: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Label(Base):
    __tablename__ = "labels"
    __table_args__ = (
        UniqueConstraint("asset_id", "ts", "label_type", name="uq_labels_asset_ts_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    label_type: Mapped[str] = mapped_column(String(128), nullable=False)
    label_value: Mapped[str] = mapped_column(String(256), nullable=False)
    label_source: Mapped[str] = mapped_column(String(128), nullable=False)


class IgnitionEvent(Base):
    __tablename__ = "ignition_events"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "event_type", "ts", "source_id", name="uq_ignition_event"
        ),
        Index("ix_ignition_events_asset_ts", "asset_id", "ts"),
        Index("ix_ignition_events_type_ts", "event_type", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class LiquidityRemovalEvent(Base):
    """On-chain LP burn/withdrawal evidence for early exit-risk scoring."""

    __tablename__ = "liquidity_removal_events"
    __table_args__ = (
        UniqueConstraint(
            "chain_slug",
            "tx_hash",
            "log_index",
            "event_kind",
            name="uq_liquidity_removal_chain_tx_log_kind",
        ),
        Index("ix_liquidity_removal_asset_ts", "asset_id", "ts"),
        Index("ix_liquidity_removal_observed_at", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    pool_id: Mapped[int] = mapped_column(ForeignKey("pools.id"), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    chain_slug: Mapped[str] = mapped_column(String(32), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.9, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class LifecycleEvent(Base):
    """Hype-lifecycle state machine transitions (SEEDING -> ... -> COLLAPSE)."""

    __tablename__ = "lifecycle_events"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "phase", "ts", "event_type", name="uq_lifecycle_asset_phase_ts"
        ),
        Index("ix_lifecycle_asset_ts", "asset_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), default="phase_transition", nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class FingerprintAssessment(Base):
    __tablename__ = "fingerprint_assessments"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "decision_ts",
            "model_version",
            name="uq_fingerprint_asset_ts_version",
        ),
        Index("ix_fingerprint_asset_ts", "asset_id", "decision_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    decision_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recidivism_score: Mapped[float] = mapped_column(Float, nullable=False)
    matched_cluster_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_wallet_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    matched_clusters: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)


class PrelaunchCandidate(Base):
    __tablename__ = "prelaunch_candidates"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "decision_ts", "model_version", name="uq_prelaunch_asset_ts"
        ),
        Index("ix_prelaunch_asset_ts", "asset_id", "decision_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    decision_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False)
    drivers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)


class Forecast(Base):
    __tablename__ = "forecasts"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "decision_ts", "model_version", name="uq_forecast_asset_ts_version"
        ),
        Index("ix_forecast_asset_ts", "asset_id", "decision_ts"),
        Index("ix_forecast_collapse", "decision_ts", "p_collapse_24h"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    decision_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    p_ignition_24h: Mapped[float] = mapped_column(Float, nullable=False)
    p_collapse_24h: Mapped[float] = mapped_column(Float, nullable=False)
    expected_hours_to_peak: Mapped[float | None] = mapped_column(Float)
    expected_hours_to_collapse: Mapped[float | None] = mapped_column(Float)
    calibration_bucket: Mapped[str | None] = mapped_column(String(32))
    calibrated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)


class NarrativeCluster(Base):
    __tablename__ = "narrative_clusters"
    __table_args__ = (
        UniqueConstraint("cluster_key", name="uq_narrative_cluster_key"),
        Index("ix_narrative_clusters_last_seen", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_key: Mapped[str] = mapped_column(String(128), nullable=False)
    seed_topic: Mapped[str] = mapped_column(String(256), nullable=False)
    mention_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class SystemHealth(Base):
    __tablename__ = "system_health"
    __table_args__ = (
        UniqueConstraint("component", "ts", name="uq_system_health_component_ts"),
        Index("ix_system_health_component_ts", "component", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component: Mapped[str] = mapped_column(String(128), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    freshness_sec: Mapped[float | None] = mapped_column(Float)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lag_sec: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)


class RpcPoolSnapshot(Base):
    """Per-endpoint RPC state captured by the worker at the end of a scan."""

    __tablename__ = "rpc_pool_snapshots"
    __table_args__ = (
        UniqueConstraint("chain_slug", "url", "ts", name="uq_rpc_pool_snapshot"),
        Index("ix_rpc_pool_snapshots_chain_ts", "chain_slug", "ts"),
        Index("ix_rpc_pool_snapshots_url_ts", "url", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_slug: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    health: Mapped[float] = mapped_column(Float, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    down: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_probe_ok: Mapped[bool | None] = mapped_column(Boolean)
    probe_count: Mapped[int] = mapped_column(Integer, nullable=False)
    probe_successes: Mapped[int] = mapped_column(Integer, nullable=False)
    probe_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    probe_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )


class IngestionWatermark(Base):
    __tablename__ = "ingestion_watermarks"
    __table_args__ = (
        UniqueConstraint("source_id", "chain_id", "cursor_name", name="uq_watermark"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    chain_id: Mapped[int | None] = mapped_column(ForeignKey("chains.id"))
    cursor_name: Mapped[str] = mapped_column(String(128), nullable=False)
    cursor_value: Mapped[str | None] = mapped_column(String(512))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ScanResult(Base):
    """One ingestion scan's pipeline stage counts and timing."""

    __tablename__ = "scan_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    pairs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    profiles: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scores: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ignition_events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fingerprints: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    forecasts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lifecycle: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    narrative: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mempool: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lp_removals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prelaunch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    catalysts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archive: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ntfy_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rpc_pool_notifications: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rpc_pool_snapshots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    cutoff_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cutoff_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)


class BacktestResult(Base):
    __tablename__ = "backtest_results"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "metric_name", "chain_slug", name="uq_backtest_result_metric_chain"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id"), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    chain_slug: Mapped[str | None] = mapped_column(String(32))
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
