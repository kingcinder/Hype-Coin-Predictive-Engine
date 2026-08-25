from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://serpent:serpent@localhost:5432/serpent"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "raw-evidence"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: str = "http://localhost:8000"

    # Streamlit GUI (single-command engine: `python -m engine`)
    ui_port: int = 8501
    ui_refresh_seconds: int = 30

    scan_interval_seconds: int = 300
    request_timeout_seconds: float = 20.0
    max_request_retries: int = 3

    target_chains_csv: str = Field(
        default="solana,base,ethereum",
        validation_alias="TARGET_CHAINS",
    )
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    base_rpc_url: str = "https://mainnet.base.org"
    ethereum_rpc_url: str = "https://ethereum-rpc.publicnode.com"

    # curated free RPC endpoint pools per chain (blueprint §2): health-scored
    # rotation and automatic failover when a public endpoint rate-limits or dies
    rpc_pool_enabled: bool = True
    solana_rpc_pool_csv: str = (
        "https://api.mainnet-beta.solana.com,"
        "https://solana-rpc.publicnode.com,"
        "https://rpc.ankr.com/solana,"
        "https://api.mainnet.rpcpool.com"
    )
    base_rpc_pool_csv: str = (
        "https://mainnet.base.org,"
        "https://base-rpc.publicnode.com,"
        "https://base.llamarpc.com,"
        "https://base.drpc.org"
    )
    ethereum_rpc_pool_csv: str = (
        "https://ethereum-rpc.publicnode.com,"
        "https://eth.llamarpc.com,"
        "https://rpc.ankr.com/eth,"
        "https://eth.drpc.org"
    )
    rpc_pool_failure_threshold: int = 2
    rpc_pool_recovery_health: float = 0.55
    rpc_pool_background_probe_enabled: bool = True
    rpc_pool_probe_interval_seconds: int = 300
    rpc_pool_alert_cooldown_seconds: int = 900

    helius_api_key: str | None = None
    alchemy_api_key: str | None = None
    quicknode_evm_rpc_url: str | None = None
    etherscan_api_key: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None

    min_discovery_liquidity_usd: float = 15_000
    min_validated_liquidity_usd: float = 50_000
    min_liquid_momentum_usd: float = 250_000
    black_min_liquidity_usd: float = 15_000
    solana_holder_scan_limit: int = 1
    solana_holder_rpc_pause_seconds: float = 0.75
    # EVM holder snapshots via free public Blockscout v2 instances (~3 req/min
    # unauthenticated), so the per-chain scan limit stays small and the pause
    # keeps the worker under the rate limit.
    evm_holder_scan_limit: int = 2
    evm_holder_rpc_pause_seconds: float = 20.0

    # radar: t0 ignition detection
    radar_ignition_pool_age_hours: int = 24
    min_ignition_liquidity_usd: float = 50_000
    radar_sniper_window_hours: int = 2
    radar_sniper_min_buys: int = 25
    radar_sniper_min_buy_sell_ratio: float = 2.5
    radar_withdrawal_window_hours: int = 24
    radar_withdrawal_drop_pct: float = 0.5
    radar_withdrawal_volume_fraction: float = 0.5
    radar_alert_quiet_hours: int = 24
    # on-chain LP burn/withdrawal watcher (EVM pair logs)
    liquidity_removal_watcher_enabled: bool = True
    liquidity_removal_lookback_blocks: int = 1_500
    liquidity_removal_max_pools: int = 200

    # fingerprint: syndicate recidivism
    fingerprint_min_cooccurrence: int = 2
    fingerprint_top_holders: int = 10
    fingerprint_learn_top_holders: int = 15
    recidivism_alert_threshold: float = 70.0

    # prelaunch queue
    prelaunch_alert_threshold: float = 60.0
    prelaunch_model_version: str = "prelaunch-rules-v1"

    # mempool: sub-minute t0 watchers
    mempool_enabled: bool = True
    mempool_burst_window_seconds: int = 120
    mempool_burst_min_txs: int = 15
    mempool_solana_poll_limit: int = 100
    evm_factory_addresses_csv: str = (
        "base:0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6,"
        "ethereum:0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
    )
    evm_lookback_hours: int = 24

    # narrative radar
    narrative_crawl_enabled: bool = True
    narrative_cluster_threshold: float = 0.6
    narrative_cluster_sig_size: int = 64
    reddit_subreddits_csv: str = "CryptoCurrency,CryptoMoonShots,SolanaMemeCoins,BaseTokens"
    youtube_channels_csv: str = ""
    github_search_queries_csv: str = "solana token launch,base memecoin,erc20 launch"
    hf_trending_enabled: bool = True
    rss_feed_urls_csv: str = ""
    narrative_endpoint_pool_enabled: bool = True
    narrative_background_probe_enabled: bool = True
    narrative_probe_interval_seconds: int = 300
    narrative_endpoint_failure_threshold: int = 2
    reddit_endpoint_pool_csv: str = "https://www.reddit.com,https://old.reddit.com"
    github_endpoint_pool_csv: str = "https://api.github.com"
    huggingface_endpoint_pool_csv: str = "https://huggingface.co"

    # telegram public-channel crawler (Telethon; free, one-time session login)
    telegram_enabled: bool = False
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_session_file: str = "telegram.session"
    telegram_channel_handles_csv: str = ""
    telegram_message_limit: int = 30
    telegram_rate_limit_pause_seconds: float = 2.0

    # ntfy.sh push notifier (free, no account on ntfy.sh)
    ntfy_enabled: bool = False
    ntfy_base_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_timeout_seconds: float = 10.0
    ntfy_backlog_hours: int = 24
    ntfy_daily_digest_enabled: bool = True
    ntfy_alert_types_csv: str = (
        "ignition_detected,liquidity_withdrawal_warning,syndicate_recidivism,lifecycle_transition"
    )

    # archive & retention: Parquet compaction of raw evidence
    archive_enabled: bool = True
    archive_backend: str = "s3"  # "s3" (MinIO) or "local" (zero-container disk)
    archive_local_dir: str = "data/archive"
    archive_compact_after_hours: float = 72.0
    archive_retention_days: int = 90
    archive_batch_size: int = 5_000
    archive_prefix: str = "evidence"
    # assumed capacity cap for the lake (bytes); used by the Archive & Retention
    # view to project a disk-full horizon from recent growth. Tune to the real
    # volume for the local disk or MinIO bucket.
    archive_lake_max_bytes: int = 100 * 1024**3  # 100 GiB

    # retention autopilot: scheduled compaction + pruning + lake-growth report
    retention_autopilot_enabled: bool = True
    retention_cadence_hours: float = 24.0
    # Bound partition fan-out per pass to smooth compaction I/O.
    retention_max_partitions_per_pass: int = 1
    # retention budget alert: warn via ntfy when the projected lake growth
    # would fill ARCHIVE_LAKE_MAX_BYTES within this many days, then re-warn
    # at most once per cooldown window.
    retention_budget_alert_days: float = 14.0
    retention_budget_alert_cooldown_hours: float = 24.0
    retention_stale_warning_after_cycles: int = 3
    retention_stale_warning_cooldown_hours: float = 24.0
    retention_backlog_warning_threshold: int = 2
    retention_backlog_warning_cooldown_hours: float = 24.0

    # lake-vs-SQL parity CI: daily comparison of the DuckDB lake read path
    # against the live SQL path over the archived evidence, paging a mismatch
    # via ntfy. PARITY_COMPARE_HOURS_AGO is the decision-time horizon for the
    # comparison and must exceed ARCHIVE_COMPACT_AFTER_HOURS +
    # RETENTION_CADENCE_HOURS so every piece of evidence at the decision time
    # is provably inside the archived lake (the module clamps it upward
    # automatically).
    parity_enabled: bool = True
    parity_frequency_hours: float = 24.0
    parity_compare_hours_ago: float = 96.0
    parity_tolerance: float = 1e-3
    parity_max_assets: int = 0  # 0 = compare every asset
    parity_alert_threshold: int = 1  # page when mismatches >= this
    parity_alert_cooldown_hours: float = 24.0
    # How long per-mismatch divergence history is kept for review before each
    # parity run prunes it (bounded history, not just the latest ntfy page).
    parity_history_retention_days: float = 90.0

    # catalyst timetable
    catalyst_alert_hours: float = 72.0

    # data lake manager: signal scoring, label densification, webhook dispatch
    data_lake_enabled: bool = True
    data_lake_signal_batch_size: int = 500
    data_lake_dense_label_forward_hours: int = 24
    webhook_enabled: bool = True
    webhook_default_cooldown_seconds: int = 300
    webhook_http_timeout_seconds: float = 10.0

    # night crawlers: expanded data sources for the engine
    nightcrawler_enabled: bool = True
    nightcrawler_interval_minutes: int = 30
    nightcrawler_coingecko_enabled: bool = True
    nightcrawler_pumpfun_enabled: bool = True
    nightcrawler_defillama_enabled: bool = True
    nightcrawler_whale_enabled: bool = True
    nightcrawler_explorer_enabled: bool = True
    nightcrawler_nitter_enabled: bool = True
    nightcrawler_presale_enabled: bool = True
    nightcrawler_farcaster_enabled: bool = True
    farcaster_search_queries_csv: str = (
        "token launch,new memecoin,presale live,dev activity,"
        "smart contract deploy,liquidity pool create,airdrop claim,"
        "NFT mint,DAO proposal,protocol upgrade,base chain launch,solana token"
    )

    # forecast layer
    forecast_enabled: bool = True
    forecast_forward_hours: int = 24
    forecast_ignition_threshold: float = 0.20
    forecast_collapse_threshold: float = -0.70
    forecast_min_samples: int = 30
    forecast_train_frequency_hours: int = 24
    forecast_drift_trailing_hours: int = 168
    forecast_drift_min_samples: int = 5
    forecast_drift_precision_margin: float = 0.15
    forecast_drift_min_precision: float = 0.4
    forecast_drift_cal_margin: float = 0.10
    forecast_drift_max_cal_error: float = 0.25
    forecast_drift_precision_fraction: float = 0.6
    forecast_drift_severe_precision: float = 0.2
    # Calibration-bias guard: when the real-only test calibration error
    # exceeds the blended (dense-label-inclusive) one by this much, emit a
    # notifier warning — dense-label interpolation may be masking true model
    # performance on observed outcomes.
    forecast_cal_gap_threshold: float = 0.10
    forecast_cal_gap_min_samples: float = 5
    forecast_cal_gap_cooldown_hours: float = 24
    # Gate forecast *usage* on the real-only test readout (not the blended one,
    # which dense-label interpolation can inflate): when the trained model is
    # untrustworthy on real observed samples — too few real test samples, or
    # real-only calibration error above a ceiling — don't emit production
    # forecasts and degrade to yellow health until the real-only numbers
    # recover. Off by default: enabling it can stop forecast production, so
    # turn it on deliberately.
    forecast_gate_on_real_metrics: bool = False
    forecast_gate_min_real_samples: float = 5
    forecast_gate_max_real_cal_error: float = 0.25

    # Alert quality guard: quiet a family after enough operator ratings show it
    # is mostly noise. An explicit re-enable control overrides the quieting.
    alert_quality_noise_floor: float = 0.25
    alert_quality_min_ratings: int = 5

    # Risk outcome tracking: observation window for evaluating flagged tokens
    risk_outcome_window_hours: float = 48.0
    risk_calibration_frequency_hours: float = 24.0
    risk_calibration_min_samples: int = 10

    model_version: str = "mvp-rules-v1"
    fingerprint_model_version: str = "cooccurrence-v1"
    forecast_model_version: str = "gbm-hist-v1"
    lifecycle_model_version: str = "lifecycle-rules-v1"

    @property
    def evm_factories(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for part in self.evm_factory_addresses_csv.split(","):
            part = part.strip()
            if not part:
                continue
            chain, _, address = part.partition(":")
            if chain and address:
                out.append((chain.strip().lower(), address.strip()))
        return out

    @property
    def reddit_subreddits(self) -> list[str]:
        return [part.strip() for part in self.reddit_subreddits_csv.split(",") if part.strip()]

    @property
    def youtube_channels(self) -> list[str]:
        return [part.strip() for part in self.youtube_channels_csv.split(",") if part.strip()]

    @property
    def github_search_queries(self) -> list[str]:
        return [part.strip() for part in self.github_search_queries_csv.split(",") if part.strip()]

    @property
    def narrative_endpoint_pools(self) -> dict[str, list[str]]:
        return {
            "reddit": [
                part.strip() for part in self.reddit_endpoint_pool_csv.split(",") if part.strip()
            ],
            "github": [
                part.strip() for part in self.github_endpoint_pool_csv.split(",") if part.strip()
            ],
            "huggingface": [
                part.strip()
                for part in self.huggingface_endpoint_pool_csv.split(",")
                if part.strip()
            ],
        }

    @property
    def ntfy_alert_types(self) -> list[str]:
        return [part.strip() for part in self.ntfy_alert_types_csv.split(",") if part.strip()]

    @property
    def telegram_channel_handles(self) -> list[str]:
        return [
            part.strip() for part in self.telegram_channel_handles_csv.split(",") if part.strip()
        ]

    @property
    def archive_backend_is_local(self) -> bool:
        return self.archive_backend.strip().lower() == "local"

    @model_validator(mode="after")
    def apply_local_single_profile(self) -> Settings:
        """Zero-container profile: SQLite + local Parquet archive.

        With ``env=local-single`` the whole engine runs on one machine with no
        Postgres/Redis/MinIO containers. The URL/backend are only swapped when
        they still hold their defaults, so an explicit override in the
        environment always wins.
        """
        if self.env == "local-single":
            # Only apply profile defaults for fields the operator did not set
            # explicitly (via init kwargs or environment variables).
            if "database_url" not in self.model_fields_set:
                self.database_url = "sqlite:///serpent.db"
            if "archive_backend" not in self.model_fields_set:
                self.archive_backend = "local"
        return self

    @property
    def rss_feed_urls(self) -> list[str]:
        return [part.strip() for part in self.rss_feed_urls_csv.split(",") if part.strip()]

    def rpc_pool_endpoints(self, chain_slug: str) -> list[str]:
        """Effective pool for a chain: the primary URL first, then the curated CSV.

        The primary is the operator's explicit override when one exists (e.g. a
        Helius key for Solana or QuickNode for EVM), otherwise the built-in
        default URL. Deduplicated, order preserved.
        """
        if chain_slug == "solana":
            csv = self.solana_rpc_pool_csv
        elif chain_slug == "base":
            csv = self.base_rpc_pool_csv
        elif chain_slug == "ethereum":
            csv = self.ethereum_rpc_pool_csv
        else:
            return []
        entries = [part.strip() for part in csv.split(",") if part.strip()]
        primary = self.rpc_url_for_chain(chain_slug)
        if primary and primary not in entries:
            entries.insert(0, primary)
        return entries

    @property
    def target_chains(self) -> list[str]:
        return [part.strip().lower() for part in self.target_chains_csv.split(",") if part.strip()]

    def rpc_url_for_chain(self, chain_slug: str) -> str | None:
        if chain_slug == "solana":
            if self.helius_api_key:
                return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
            return self.solana_rpc_url
        if chain_slug == "base":
            return self.quicknode_evm_rpc_url or self.base_rpc_url
        if chain_slug == "ethereum":
            return self.quicknode_evm_rpc_url or self.ethereum_rpc_url
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
