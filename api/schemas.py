from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class HealthComponent(BaseModel):
    component: str
    state: str
    ts: datetime
    message: str | None = None
    freshness_sec: float | None = None
    lag_sec: float | None = None
    error_count: int


class HealthResponse(BaseModel):
    status: str
    database: str
    components: list[HealthComponent]


class TokenScoreRow(BaseModel):
    asset_id: int
    chain: str
    address: str
    symbol: str
    name: str | None
    decision_ts: datetime
    hype: float
    ethos: float
    risk: float
    liquidity_access: float
    manipulation: float
    confidence: float
    uncertainty: float
    catalyst: float
    exit_risk: float
    research_priority: float
    risk_band: str


class FeatureRow(BaseModel):
    name: str
    value: float
    missing: bool
    freshness_score: float
    source_count: int


class TokenDetail(BaseModel):
    asset_id: int
    chain: str
    address: str
    symbol: str
    name: str | None
    status: str
    website_url: str | None
    github_url: str | None
    latest_score: TokenScoreRow | None
    features: list[FeatureRow]
    explanation: dict[str, Any] | None


class AlertRow(BaseModel):
    id: int
    asset_id: int
    symbol: str | None
    created_at: datetime
    alert_type: str
    state: str
    message: str
    notified_at: datetime | None
    acked_at: datetime | None
    ack_quality: str | None


class AlertAckRequest(BaseModel):
    """Operator acknowledgement of an open alert.

    ``quality`` feeds the signal-quality ledger: ``"useful"`` when the alert
    led to a good call, ``"noise"" when it did not, or ``None`` for a plain
    ack.
    """

    quality: str | None = None


class AlertQualityRow(BaseModel):
    id: int
    asset_id: int
    symbol: str | None
    alert_type: str
    state: str
    message: str
    created_at: datetime
    acked_at: datetime | None
    ack_quality: str | None


class AlertQualityLedger(BaseModel):
    """Operator signal-quality ledger: how often acked alerts were useful."""

    total_acked: int
    useful: int
    noise: int
    unrated: int
    useful_rate: float | None
    recent: list[AlertQualityRow]


class RiskResponse(BaseModel):
    asset_id: int
    risk_band: str
    risk_score: float
    reasons: list[str]
    hard_reject: bool


class BacktestResultRow(BaseModel):
    run_id: int
    status: str
    started_at: datetime
    cutoff_start: datetime
    cutoff_end: datetime
    model_version: str
    metrics: dict[str, float]


class SimilarSetupRow(BaseModel):
    asset_id: int
    chain: str
    address: str
    symbol: str
    name: str | None
    decision_ts: datetime
    similarity_score: float
    distance: float
    features_compared: int
    hype: float | None
    risk_band: str | None
    research_priority: float | None


class IgnitionEventRow(BaseModel):
    id: int
    asset_id: int
    symbol: str | None
    chain: str
    event_type: str
    ts: datetime
    observed_at: datetime
    confidence: float
    details: dict[str, Any]


class FingerprintRow(BaseModel):
    id: int
    asset_id: int
    symbol: str | None
    chain: str
    decision_ts: datetime
    recidivism_score: float
    matched_cluster_count: int
    matched_wallet_count: int
    matched_roles: list[str]
    matched_clusters: list[dict[str, Any]]


class PrelaunchRow(BaseModel):
    id: int
    asset_id: int
    symbol: str | None
    chain: str
    decision_ts: datetime
    priority_score: float
    drivers: dict[str, Any]


class ForecastRow(BaseModel):
    id: int
    asset_id: int
    symbol: str | None
    chain: str
    decision_ts: datetime
    p_ignition_24h: float
    p_collapse_24h: float
    expected_hours_to_peak: float | None
    expected_hours_to_collapse: float | None
    calibration_bucket: str | None
    calibrated: bool
    model_version: str
    details: dict[str, Any]


class LifecycleEventRow(BaseModel):
    id: int
    asset_id: int
    symbol: str | None
    chain: str
    phase: str
    event_type: str
    ts: datetime
    observed_at: datetime
    confidence: float
    details: dict[str, Any]


class LifecycleTransitionAlertRow(BaseModel):
    id: int
    asset_id: int
    symbol: str | None
    chain: str
    phase: str
    event_id: int
    event_ts: datetime
    confidence: float
    created_at: datetime
    state: str
    message: str
    evidence: dict[str, Any]


class NarrativeClusterRow(BaseModel):
    id: int
    cluster_key: str
    seed_topic: str
    mention_count: int
    first_seen_at: datetime
    last_seen_at: datetime


class CatalystRow(BaseModel):
    id: int
    asset_id: int
    symbol: str | None
    catalyst_type: str
    scheduled_at: datetime | None
    published_at: datetime | None
    confidence: float


class ArchiveManifestRow(BaseModel):
    id: int
    object_key: str
    source_name: str
    partition_year: int
    partition_month: int
    row_count: int
    byte_size: int
    first_observed_at: datetime
    last_observed_at: datetime
    created_at: datetime


class RetentionRunRow(BaseModel):
    id: int
    ts: datetime
    partitions: int
    archived_rows: int
    byte_size: int
    compacted: int
    pruned: int
    growth_bytes: int
    growth_pct: float | None
    duration_sec: float | None


class RetentionGrowthRow(BaseModel):
    """Retention-pass history plus a projected disk-full horizon."""

    runs: list[RetentionRunRow]
    max_bytes: int
    growth_rate_bytes_per_hour: float
    projected_full_at: datetime | None
    days_to_full: float | None
    pct_full: float


class RpcPoolProbeRow(BaseModel):
    ts: datetime
    ok: bool


class RpcPoolEndpointRow(BaseModel):
    url: str
    health: float
    consecutive_failures: int
    down: bool
    last_probe_at: datetime | None
    last_probe_ok: bool | None
    probe_count: int
    probe_successes: int
    probe_failures: int
    probe_history: list[RpcPoolProbeRow]


class RpcPoolChainRow(BaseModel):
    chain: str
    state: str
    down_count: int
    degraded_count: int
    endpoints: list[RpcPoolEndpointRow]


class VelocityFeatureRow(BaseModel):
    asset_id: int
    symbol: str
    chain: str
    decision_ts: datetime
    kol_velocity: float | None
    kol_velocity_missing: bool
    github_star_velocity: float | None
    github_star_velocity_missing: bool
    hf_download_velocity: float | None
    hf_download_velocity_missing: bool


class ScanResultRow(BaseModel):
    id: int
    ts: datetime
    duration_sec: float | None
    pairs: int
    profiles: int
    scores: int
    ignition_events: int
    fingerprints: int
    forecasts: int
    lifecycle: int
    narrative: int
    mempool: int
    lp_removals: int
    prelaunch: int
    catalysts: int
    archive: int
    ntfy_sent: int
    rpc_pool_notifications: int
    rpc_pool_snapshots: int
    state: str
    error_message: str | None
    details: dict[str, Any]


class NotifierHealthRow(BaseModel):
    component: str
    state: str
    ts: datetime
    message: str | None
    error_count: int


class OpsConsoleResponse(BaseModel):
    last_scan: ScanResultRow | None
    notifier_health: NotifierHealthRow | None
    recent_alerts: list[AlertRow]


class EngineScanProgressRow(BaseModel):
    phase: str
    phase_message: str
    duration_sec: float | None
    iteration: int
    pairs: int
    scores: int
    forecasts: int
    lifecycle: int
    narrative: int
    catalysts: int
    ignition_events: int
    fingerprints: int
    archive: int
    ntfy_sent: int
    rpc_pool_snapshots: int
    error_message: str | None


class EngineStatusResponse(BaseModel):
    status: str
    uptime_sec: float | None
    total_iterations: int
    scan_interval_seconds: int
    scan: EngineScanProgressRow


class SeedResponse(BaseModel):
    status: str
    message: str


class TriggerResponse(BaseModel):
    status: str
    message: str


class SignalScoreRow(BaseModel):
    source_table: str
    record_id: int
    signal_score: float
    novelty_score: float
    corroboration_score: float
    temporal_score: float
    magnitude_score: float
    reasons: list[str]
    actionable: bool


class SignalBatchResultRow(BaseModel):
    total_scored: int
    actionable_count: int
    noise_count: int
    avg_signal: float
    top_signals: list[SignalScoreRow]
    timestamp: str


class LabelProgressRow(BaseModel):
    total_labels: int
    ignition_positive: int
    ignition_negative: int
    collapse_positive: int
    collapse_negative: int
    min_samples_required: int
    progress_pct: float
    ready_to_train: bool
    unique_assets_labeled: int
    assets_with_snapshots: int
    shortfall: int


class DataLakePassResult(BaseModel):
    signal: dict[str, Any]
    labels: dict[str, Any]
    webhooks: dict[str, Any]
    progress: dict[str, Any]


class ConfidenceDashboardResponse(BaseModel):
    label_progress: LabelProgressRow
    scoring_breakdown: list[dict[str, Any]]
    scan_history: list[dict[str, Any]]


class WebhookConfigRow(BaseModel):
    id: int
    url: str
    name: str
    event_types: list[str]
    enabled: bool
    cooldown_seconds: int
    chain_filter: str | None
    min_signal_score: float
    last_dispatched_at: datetime | None
    created_at: datetime


class WebhookRegisterRequest(BaseModel):
    url: str
    name: str
    event_types: list[str] | None = None
    secret: str | None = None
    enabled: bool = True
    cooldown_seconds: int = 300
    chain_filter: str | None = None
    min_signal_score: float = 0.0


class WebhookDispatchRow(BaseModel):
    id: int
    webhook_config_id: int
    event_type: str
    dispatched_at: datetime
    success: bool
    status_code: int | None
    error_message: str | None
    duration_ms: float | None
