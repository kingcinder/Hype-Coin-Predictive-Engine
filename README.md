# Serpent Circle Hype-Coin Predictive Engine

Local-first research intelligence for speculative Solana, Base, and Ethereum tokens. It separates hype from structural danger and stores point-in-time evidence so scans can be replayed without future leakage.

This is not an auto-trader, wallet, custody system, or guaranteed-profit bot.

## One-command startup

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Then open:

- API health: http://localhost:8000/health
- Streamlit dashboard: http://localhost:8501
- MinIO console: http://localhost:9001

The MVP runs in free-first mode. Optional provider keys in `.env` improve reliability and coverage, especially for Solana and EVM explorer data.

## Single-command engine (one process, GUI included)

The whole engine resolves into one command — no containers, no three terminals:

```bash
python -m engine
```

This one process bootstraps the SQLite database (idempotent), then runs the
ingestion worker loop, the REST API on http://localhost:8000, and the Streamlit
GUI on http://localhost:8501 together. The GUI opens on a compact **Command
Center** view that auto-refreshes every 30 seconds (`UI_REFRESH_SECONDS`), and
whatever view you select re-renders on the same cadence, so the board stays
live while the worker scans. Ctrl+C stops the worker, API, and GUI cleanly.
Equivalent Makefile target: `make engine`.

## Zero-container local profile (no Docker, no containers)

The whole engine — scanner, radar, forecast, archive, API, UI — runs on one machine with SQLite and a local Parquet lake (the single-command engine above is this profile, automated):

```bash
python scripts/bootstrap_local.py        # creates serpent.db + data/archive, seeds reference data
python -m ingestion.worker --loop        # scan + radar + fingerprint + forecast + archive
uvicorn api.main:app --host 0.0.0.0 --port 8000
streamlit run ui/app.py --server.port=8501
```

Or use the Makefile targets (`make bootstrap-local`, `make local-worker`, `make local-api`, `make local-ui`). Raw evidence is compacted into partitioned Parquet and pruned past the retention window; the lake is queryable with DuckDB:

```bash
python -m ops.archive --once
python -m ops.archive --query "SELECT source_type, count(*) AS n FROM evidence GROUP BY 1 ORDER BY n DESC"
```

## DuckDB lake feature read path

The market/liquidity feature block can be computed directly from the archived Parquet lake via DuckDB (`features/lake.py`) instead of hammering the hot DB — the same h1|h24|m5 window rules, hourly floor, and first-wins dedup as ingestion, feeding the *shared* feature math the SQL path uses. The parity test seeds identical data into both paths and asserts the values match feature-for-feature:

```bash
python -m pytest tests/test_lake_features.py -q
```

## Retention autopilot

Compaction and pruning also run as a scheduled job on a configurable cadence (`RETENTION_CADENCE_HOURS`, default 24h): the APScheduler entrypoint registers it, the `--loop` worker checks whether a pass is due after each scan, and standalone boxes can drive it with the included OS schedulers — `deploy/systemd/serpent-retention.timer` + `.service` on Linux, or `scripts/install_retention_task.ps1` for Windows Task Scheduler. Each pass persists a `RetentionRun` (lake totals + growth since the last pass) and reports the growth in **Feed Health** as the `lake` component:

```bash
python -m ops.retention --once          # run one pass now
python -m ops.retention --check-due     # exit 0 when a pass is due (script-friendly)
```

## RPC resilience: health-scored endpoint pool

Free public RPCs rate-limit and die — the engine treats that as a rotation problem, not a failure. Every chain gets its own health-scored pool (`SOLANA_RPC_POOL_CSV`, `BASE_RPC_POOL_CSV`, `ETHEREUM_RPC_POOL_CSV`): a failed request decays the endpoint's health, `RPC_POOL_FAILURE_THRESHOLD` consecutive failures take it down, and a background daemon probe thread per chain (every `RPC_POOL_PROBE_INTERVAL_SECONDS`, plus a probe pass each scan) health-checks downed endpoints — `getHealth` for Solana, `eth_blockNumber` for EVM — so they recover on their own, no waiting for traffic. When a pool reaches zero healthy endpoints, or one endpoint remains down past `RPC_POOL_ALERT_COOLDOWN_SECONDS` (default 15 minutes), the ntfy notifier sends one deduplicated operational push and retries failed deliveries. Both the Solana client and the EVM factory watcher feed their chain's health. The **RPC Pool Status** UI view shows the latest persisted per-endpoint health, failures, probe counters, and down states per chain, so separate API and worker processes agree; the aggregate `component:rpc_pool:{chain}` rows live under Feed Health. Refresh curated CSVs after an outage with `python scripts/refresh_rpc_pools.py --dry-run`, then `python scripts/refresh_rpc_pools.py` to probe every endpoint and rewrite `.env` with healthy entries only; effective primary overrides are preserved separately.

## On-chain LP removal watcher

The ingestion scan watches known Base and Ethereum pool contracts for Uniswap-v2 `Burn` events and LP-token transfers to the zero address. Each removal is persisted by transaction/log identity, survives process restarts, and feeds the `lp_removal_signal` feature before lifecycle evaluation. The risk engine raises early `ExitRiskScore` and adds a hard reject when a fresh LP removal hits a shallow book, so the warning arrives before the token reaches COLLAPSE. Configure the watcher with `LIQUIDITY_REMOVAL_WATCHER_ENABLED`, `LIQUIDITY_REMOVAL_LOOKBACK_BLOCKS`, and `LIQUIDITY_REMOVAL_MAX_POOLS`; Solana/other AMM-specific parsers can be added without changing the normalized signal.

## Narrative velocity features

The narrative radar also computes dev-activity proxies from the crawler metrics it already stores: `kol_velocity` (distinct KOL channels mentioning a token in 24h), `github_star_velocity` and `hf_download_velocity` (per-day star/download growth of the token's repos and models, from the raw-evidence crawl history). These feed the hype and catalyst scores, and the **Narrative Dev-Activity** UI view tracks them live per token (missing evidence renders as `missing`, never a fake zero), refreshing every 30 seconds by default (`NARRATIVE_DEV_ACTIVITY_REFRESH_SECONDS`). Reddit, GitHub, and HuggingFace use configurable endpoint pools (`REDDIT_ENDPOINT_POOL_CSV`, `GITHUB_ENDPOINT_POOL_CSV`, `HUGGINGFACE_ENDPOINT_POOL_CSV`); every crawl batch probes all candidates first, skips down endpoints, and keeps background recovery probes running.

## Forecast retraining cadence

Forecast labels, classifiers, hazard curves, and the drift baseline are retrained from persisted point-in-time features every `FORECAST_TRAIN_FREQUENCY_HOURS` (default: 24). The first scan trains immediately when enough labeled history exists; later runs are cadence-gated by the persisted completed `BacktestRun`, so the APScheduler process and a separate worker cannot retrain more often than configured. The worker loop and `ingestion.scheduler` both drive the same database-backed gate, and every completed training run re-persists `forecast.drift.*` metrics for the Backtest & Drift view.

To measure whether the narrative proxies add predictive value, run a replay-safe A/B experiment on the same labeled corpus:

```bash
python -m forecast.experiment
# or: make forecast-ab DECISION_TS=2026-08-20T00:00:00Z
```

It fits the full feature set and a second model with `kol_velocity`, `github_star_velocity`, and `hf_download_velocity` neutralized, then prints and persists precision-at-10, calibration error, median lead time, and masked-minus-full deltas. The experiment does not overwrite production forecasts; its `forecast_ab.*` metrics appear in Backtest & Drift.

## Optional: phone push via ntfy.sh

Free push notifications, no account needed. Set a unique topic and subscribe on your phone (the ntfy app or https://ntfy.sh/<topic>):

```powershell
# .env
NTFY_ENABLED=True
NTFY_TOPIC=your-unique-topic
```

Each scan pushes the t0 alert types — ignition events (sniper bursts, first
liquidity injections), liquidity withdrawal warnings, syndicate recidivism, and
`lifecycle_transition` (the moment a token reaches COLLAPSE / RUGGED / DEAD, at
maximum priority) — once each, with severity-tagged phone notifications. The
notifier also sends one durable UTC-day digest (`NTFY_DAILY_DIGEST_ENABLED=True`)
covering every terminal transition and ignition from the preceding 24 hours in
a single message. Failed alert and digest pushes are retried on the next scan;
nothing is re-pushed twice.

## Optional: Telegram public channels

The narrative radar can also crawl **public Telegram channels** through Telethon (free; the craft stays ToS-safe: public broadcast channels only, read-only, rate-limited). Setup is a one-time interactive login:

```powershell
pip install -e ".[telegram]"
# set TELEGRAM_ENABLED=True, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHANNEL_HANDLES_CSV=@channel1,@channel2
python scripts/telegram_auth.py
```

The session file (`telegram.session`) is gitignored. Without credentials or an authorized session the crawler is skipped or reports health-yellow — it never blocks the pipeline.

## Development shortcuts

### Bash (Linux / macOS)

```bash
./scripts/dev.sh install       # install project
./scripts/dev.sh engine        # start everything
./scripts/dev.sh api           # start API server
./scripts/dev.sh ui            # start Streamlit dashboard
./scripts/dev.sh test          # run tests
./scripts/dev.sh lint          # lint + type check
./scripts/dev.sh help          # full list of commands
```

### PowerShell (Windows)

```powershell
.\scripts\dev.ps1 setup
.\scripts\dev.ps1 test
.\scripts\dev.ps1 smoke
```

## Scores

The engine keeps separate score channels:

- `HypeScore`
- `EthosScore`
- `RiskScore`
- `LiquidityAccessScore`
- `ManipulationScore`
- `ConfidenceScore`
- `UncertaintyScore`
- `CatalystScore`
- `ExitRiskScore`
- `ResearchPriorityScore`

`UncertaintyScore` also incorporates the tracked asset's chain RPC pool: degraded endpoint health lowers confidence and raises uncertainty proportionally, while an all-down pool applies the maximum data-layer penalty. The forecast feature matrix carries `kol_velocity`, `github_star_velocity`, `hf_download_velocity`, and `rpc_pool_health`; low live RPC health shrinks forecast probabilities toward an honest 50/50 baseline. Each forecast persists local per-feature impacts against a neutral/missing baseline, and the **Forecast** UI shows the velocity features plus the top drivers for both ignition and collapse probabilities. `BLACK` risk is a hard reject and cannot be promoted into the actionable research queue even when hype is high.

Each tracked token moves through a hype-lifecycle state machine (`SEEDING → IGNITION → PARABOLIC → SATURATION → COLLAPSE`, exits `DEAD` / `RUGGED` / `SURVIVOR`). The `lifecycle_phase` feature feeds risk scoring **and the forecast model**: the survival layer conditions time-to-collapse / time-to-peak on the token's current phase — training fits one hazard curve per phase and each forecast records which curve it used (`hazard_phase`), so a token already in COLLAPSE decays on a fast curve while a SEEDING token rides the long tail. The **Lifecycle Radar** UI view shows current phases and transitions, including terminal `lifecycle_transition` alerts with their inline one-hour return, withdrawal, liquidity, and other persisted phase evidence.

## Backtesting

```bash
python -m backtest.runner --start 2026-05-01T00:00:00Z --forward-hours 24
```

Runs replay-safe scans and records `git_sha`, precision-at-10, median ignition-lead minutes, collapse-warning lead minutes, and false-alarm rate. Metrics are visible in the **Backtest & Drift** UI view.

The lifecycle state machine has its own walk-forward backtest that scores **each phase transition** against realized forward prices — does an IGNITION transition actually predict a +20% pump (and how many minutes early)? Does a COLLAPSE transition predict the -70% crash? — with per-type precision, false-alarm rate, and median lead time:

```bash
python -m pump_physics.backtest --start 2026-05-01T00:00:00Z --step-hours 6 --forward-hours 48
```

## API

- `GET /health`
- `GET /tokens/hot`
- `GET /tokens/{id}`
- `GET /tokens/{id}/similar`
- `GET /alerts`
- `GET /scores/top`
- `GET /risk/{id}`
- `GET /backtest/results`
- `GET /radar/ignitions`
- `GET /radar/prelaunch`
- `GET /fingerprint/top`
- `GET /fingerprint/{asset_id}`
- `GET /forecasts`
- `GET /narrative/clusters`
- `GET /catalysts`
- `GET /archive/manifests`
- `GET /retention/runs` (retention-autopilot history: lake totals + growth per pass)
- `GET /lifecycle/current`
- `GET /lifecycle/events`
- `GET /lifecycle/alerts`
- `GET /rpc/pool`
- `GET /features/velocity`
- `GET /ops/console`

## Development proof path

```powershell
.\scripts\dev.ps1 setup
.\scripts\dev.ps1 test
docker compose up --build
```

For fixture-only verification without live network calls:

```powershell
.\scripts\dev.ps1 smoke
```
