# Validation Runbook

## Local Checks

```powershell
.\scripts\dev.ps1 setup
.\scripts\dev.ps1 test
.\scripts\dev.ps1 smoke
```

## Phase 9 — Benchmark Harness (does the engine beat a dumb baseline?)

The `validation/` package is a standalone, leak-first benchmark harness. It
observes and measures the engine; it never writes to the engine database.

```bash
# Synthetic self-tests: perfect predictor, pure noise, injected leakage.
# The injected-leak case must be FLAGGED, not reported as a great score.
python -m validation --self-test

# Benchmark a real engine DB (read-only), write a versioned report:
python -m validation --db serpent.db
# -> reports/validation-<ts>.json (report format v1)
```

Artifacts, in dependency order:

- `docs/validation-methodology.md` — Stage 1: literature review (walk-forward /
  purged / embargoed CV, calibration metrics, precision@K, regime stratification,
  meme-coin failure modes, survival analysis), metric mapping to every engine
  output, ground-truth definitions, naive-baseline + significance gates.
- `docs/validation-harness-design.md` — Stage 2: architecture (reuses the
  point-in-time evidence architecture), walk-forward partition + embargo/purge,
  versioned report format, and the pre-committed self-test expectations.
- `validation/` — Stage 3: the harness (metrics, baselines, leakage detection,
  synthetic datasets, report writer, CLI). Regression tests live in
  `tests/test_validation_harness.py`.
- `docs/validation-field-report.md` — Stage 4: the honest verdict on the real
  engine, cross-checked against the versioned report JSON.

Self-test expectations are pre-committed in the design doc; the pytest
regression suite (`tests/test_validation_harness.py`) re-runs all three
synthetic cases on every CI pass so a broken or gameable metric cannot slip
back in silently.

## Production deployment checks

On Linux, install with `sudo REPO_URL=<git-url> bash deploy/install.sh`.
Review `/opt/serpent/.env` before restarting the API, worker, and UI. Verify:

```bash
systemctl is-enabled serpent-api serpent-worker serpent-ui serpent-retention.timer
systemctl --no-pager status serpent-api serpent-worker serpent-ui serpent-retention.timer
curl -fsS http://127.0.0.1:8000/health
journalctl -u serpent-worker --since today --no-pager
```

Updates use `sudo bash /opt/serpent/deploy/update.sh`; it requires a clean,
fast-forwardable checkout, applies migrations before restarting services, and
leaves the database and archive in place. `deploy/uninstall.sh` removes units
and services but retains data unless `REMOVE_DATA=true` is explicitly set.

## Docker Checks

```powershell
Copy-Item .env.example .env -ErrorAction SilentlyContinue
docker compose up --build
```

Confirm:

- `GET http://localhost:8000/health` returns `ok`.
- `http://localhost:8501` renders the dashboard.
- Worker logs show either successful scans or explicit source failure reasons.
- `system_health` rows exist for `api`, `worker`, and configured sources.

## Backtest Leakage Rule

Backtests must only read source, feature, score, and market rows with `observed_at <= decision_time`. A late-arriving record can be used in future decisions, not retroactively in historical scans.

For a full catalog of look-ahead / leakage vectors in the feature and label
pipelines — including the model-output `collapse_probability_24h` feedback loop,
non-point-in-time static asset metadata, and the bootstrap-label
`observed_at` gap — see `docs/leakage-audit.md`.

## Source Normalization Proof

Venue-source health must mean normalized rows were written, not only that an HTTP response was received.

- DexScreener pair ingestion must create idempotent assets, pools, pairs, market snapshots, and liquidity snapshots.
- GeckoTerminal new-pool ingestion must create normalized assets, pools, pairs, market snapshots, and liquidity snapshots, and `source:geckoterminal_new_pools` health must report the normalized pool count.

## Explanation And Similarity Proof

- A token with two scored feature snapshots must store non-empty `changed_features` when meaningful feature values move.
- `GET /tokens/{id}/similar` must return only stored feature vectors with enough comparable fields; it must return an empty list when history is insufficient instead of pretending to have similar setups.

## Ignition Radar Proof

- A pool created within `RADAR_IGNITION_POOL_AGE_HOURS` whose first liquidity snapshot meets `MIN_IGNITION_LIQUIDITY_USD` must produce one idempotent `first_liquidity_injection` event.
- A pool with a buy burst above `RADAR_SNIPER_MIN_BUYS` and `RADAR_SNIPER_MIN_BUY_SELL_RATIO` must produce one idempotent `sniper_burst` event.
- A discrete liquidity drop above `RADAR_WITHDRAWAL_DROP_PCT` that cannot be explained by window volume (`RADAR_WITHDRAWAL_VOLUME_FRACTION`) must produce one `liquidity_withdrawal` event; a drop covered by matching volume must not.
- Each new event must create an idempotent alert (`ignition_detected` or `liquidity_withdrawal_warning`) and re-scanning must not duplicate rows.
- `component:radar` health must report scan state.

## Syndicate Fingerprint Proof

- Wallets co-occurring in the same token's launch set across at least `FINGERPRINT_MIN_COOCCURRENCE` assets must form one `WalletCluster` with role assignments (deployer / sniper / lp_remover / unknown).
- A token whose launch wallets overlap a cluster with toxic history (RED/BLACK scores or liquidity withdrawals) must receive a `recidivism_score` above zero with `matched_cluster_count` > 0; a token with no overlap must receive `0.0`.
- Recidivism >= `RECIDIVISM_ALERT_THRESHOLD` must create an idempotent `syndicate_recidivism` alert.
- `liquidity_withdrawal_signal`, `lp_removal_signal`, and `recidivism_score` must appear as features and feed the risk engine. EVM `Burn` logs and LP-token transfers to the zero address must persist idempotent removal events keyed by chain/transaction/log/kind; a fresh LP removal must raise early exit risk before COLLAPSE and a removal on a shallow book must be a hard reject.
- `component:fingerprint` health must report assessment state.

## Mempool Proof

- Solana: a mint whose pool was created within `MEMPOOL_BURST_WINDOW_SECONDS` and which receives at least `MEMPOOL_BURST_MIN_TXS` new signatures must produce one `sniper_burst` ignition event; re-watching the same signatures must not duplicate events or evidence.
- EVM: a `PairCreated` log on a configured factory must create idempotent asset/contract/pool/pair rows plus initial market and liquidity snapshots, with a `source:evm_mempool` health row.

## Prelaunch Queue Proof

- An asset without a tradable pool must be ranked into `prelaunch_candidates`; an asset with a pool must not be.
- Ranking above `PRELAUNCH_ALERT_THRESHOLD` must create one idempotent `prelaunch_candidate` alert.

## Narrative Radar Proof

- Free crawlers (Reddit public JSON, YouTube RSS, GitHub search, HuggingFace trending, RSS) must persist mentions/news rows with per-source health.
- Reddit, GitHub, and HuggingFace endpoint pools must probe every configured endpoint before each crawl batch, skip endpoints that fail the probe, mark fetch failures against the selected endpoint, and run idempotent background recovery probes at `NARRATIVE_PROBE_INTERVAL_SECONDS`. A batch with no healthy endpoints is skipped with yellow source health rather than issuing a known-dead request.
- Mentions sharing vocabulary above `NARRATIVE_CLUSTER_THRESHOLD` must land in the same `narrative_cluster`; a repeated cluster run must not re-cluster already-clustered mentions.
- `narrative_cluster_growth_7d`, `shill_channel_diversity`, and `prelaunch_narrative_velocity` must appear as features.

## Narrative Velocity Features Proof (blueprint §6)

- `kol_velocity` must equal the number of distinct KOL identities (YouTube channel ids and Telegram channel handles) whose mention of the symbol is newer than 24h; mentions older than 24h must not count.
- `github_star_velocity` must be the max across the symbol's repos of (stars at the latest crawl − stars at the earliest crawl) ÷ elapsed days, computed only from `RawEvidenceItem` crawls with `observed_at <= decision_ts`; a repo observed only once must not produce a value (feature reports missing).
- `hf_download_velocity` follows the same rule from HuggingFace download counts.
- All three must be persisted as `Feature` rows on a scoring scan and feed scores (KOL breadth into hype; star/download velocity into catalyst).
- `GET /features/velocity` must return the latest per-asset velocity values with honest `missing` flags, and the **Narrative Dev-Activity** UI view must render them (table plus star-velocity chart).

## Live Ops Console Proof

- The worker must persist a `ScanResult` row at the end of each successful scan, including pipeline stage counts (profiles, pairs, mempool, lp_removals, prelaunch, narrative, catalysts, ignitions, fingerprints, lifecycle, forecasts, scores, archive, ntfy_sent, rpc_pool_notifications, rpc_pool_snapshots), scan duration, and state. The scan must report `archive=0` (stage skipped): the worker never compacts — the retention autopilot owns the archive.
- A failed scan must persist a `ScanResult` row with `state=red` and the error message.
- `GET /ops/console` must return the latest scan result, notifier health, and recently pushed alerts with timestamps.
- The **Live Ops Console** UI view must render pipeline stage counts, notifier health status, and a table of recent pushed alerts.
- Notifier health must reflect the latest notifier flush result (sent/failed/pending counts, digest status).
- Recent alerts must be ordered by `notified_at` descending and limited to the 20 most recent.

## RPC-Aware Score Uncertainty Proof

- Scoring must resolve each asset's chain and apply the corresponding RPC pool's effective availability (healthy endpoint health scores, down endpoints as zero) to `UncertaintyScore`.
- A degraded pool must lower `ConfidenceScore` and raise `UncertaintyScore`; an all-down pool must apply the maximum data-layer penalty. Disabling RPC pooling must leave the configured single-source scoring behavior unchanged.
- `FORECAST_FEATURE_NAMES` must include `kol_velocity`, `github_star_velocity`, `hf_download_velocity`, and `rpc_pool_health`; low live RPC health must shrink forecast probabilities toward 0.5 rather than present degraded data as confident predictions.

## RPC Endpoint Pool Proof (blueprint §2)

- `pick()` must prefer the healthiest endpoint and round-robin on ties (least-recently-used); a failed request must decay that endpoint's health so the next pick fails over.
- Each chain (solana, base, ethereum) must have its own pool built from its `*_RPC_POOL_CSV` with the effective primary URL prepended (e.g. a Helius/QuickNode override when configured); pools must be distinct cached singletons.
- `RPC_POOL_FAILURE_THRESHOLD` consecutive failures must take an endpoint down (excluded from picks, flagged in the `component:rpc_pool:{chain}` health row). A successful probe must jump-start it to `RPC_POOL_RECOVERY_HEALTH`.
- The background probe thread (started by the worker and scheduler, interval `RPC_POOL_PROBE_INTERVAL_SECONDS`) must recover a downed endpoint on the timer alone — with zero `pick()` calls — and failed probes must keep it down while decaying health further. `start_background_probe` must be idempotent and `stop_background_probe` must join the thread; while the thread runs, the 20-pick probe slot must be disabled, and it must return once stopped.
- The synchronous `probe_down_endpoints` pass (run each scan before the pool health rows) must recover downed endpoints the same way and must never raise, even when the probe itself raises.
- The probe must dispatch per chain: Solana via `getHealth`, EVM chains via `eth_blockNumber`.
- EVM factory watcher RPC calls must mark their chain's pool endpoint a success/failure exactly like the Solana client, so EVM traffic feeds `rpc_pool:base` / `rpc_pool:ethereum` health.
- `record_pool_health` must write one row per chain (`rpc_pool:solana`, `rpc_pool:base`, `rpc_pool:ethereum`), red when any endpoint is down in that chain.
- With ntfy enabled, a pool reaching zero healthy endpoints emits one deduplicated `RPC Pool Exhausted` push; an endpoint still down after `RPC_POOL_ALERT_COOLDOWN_SECONDS` emits one `RPC Endpoint Down` push. Failed pushes remain eligible for retry, and recovery resets both cooldowns.
- The worker must persist one `rpc_pool_snapshots` row per endpoint per scan, including endpoint state, probe counters, and bounded history. `GET /rpc/pool` must prefer the latest persisted snapshot so separate API and worker processes agree; before the first worker scan it may fall back to local state. The **RPC Pool Status** and **Feed Health** UI views must render live per-chain endpoint states and recent probe history.
- With every endpoint down, the pool must keep trying the least-recently-used endpoint rather than raising.
- `SolanaRpcClient` must mark success/failure on the pooled endpoint per request, and a client constructed with `pool_enabled=False` must fall back to the configured single URL.

## Telegram Public-Channel Proof

- The Telegram crawler must be skipped entirely (no network, no health row) unless `TELEGRAM_ENABLED` is true with both `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` configured and at least one channel handle.
- Only public broadcast channels may be read: the entity must have a `username` and `broadcast == True`. Private chats and groups are skipped.
- Message normalization must be a pure function: text, `t.me/{channel}/{id}` URL, date, sender id, views, and forwards.
- An unauthorized session must raise a descriptive error that the pipeline records as `source:telegram` health red — never a silent partial crawl.
- Channel fetches are rate-limited by `TELEGRAM_RATE_LIMIT_PAUSE_SECONDS` plus Telethon's built-in flood-sleep.

## Catalyst Timetable Proof

- A news item mentioning TGE/airdrop/unlock/listing terms with a parseable date must create one idempotent `Catalyst` row.
- A catalyst scheduled within `CATALYST_ALERT_HOURS` must create one idempotent `upcoming_catalyst` alert; catalysts outside the window must not.

## Forecast Proof

- Labels are only written when the full forward window is in the past relative to generation time (no future leakage).
- With at least `FORECAST_MIN_SAMPLES` labeled samples the engine must train, calibrate (isotonic), fit the discrete hazard model, persist `forecast.*` backtest metrics, and write `Forecast` rows per asset with a calibration bucket.
- The first completed forecast training run is due immediately; after that, `FORECAST_TRAIN_FREQUENCY_HOURS` is enforced from the persisted completed forecast `BacktestRun`, not process memory. Both `ingestion.scheduler` and worker `--loop` use the same cadence gate, and every completed retraining run persists a fresh `forecast.drift.*` baseline.
- `python -m forecast.experiment` must train full and velocity-masked variants on the same chronological/purged labeled split, report precision-at-10, calibration error, and median lead time for each, and persist masked-minus-full deltas as `forecast_ab.*` metrics without overwriting production `Forecast` rows.
- The forecast feature set (`FORECAST_FEATURE_NAMES`) must include the narrative dev-activity proxies `kol_velocity`, `github_star_velocity`, and `hf_download_velocity`, plus `lifecycle_phase` (0=seeding..4=collapse, 5=terminal), and populated values must reach the training matrix (missing features enter as honest zeros). Re-running training on a labeled corpus with velocity features populated must re-persist the drift baseline (`forecast.drift.*`).
- **Phase-dependent hazards:** training must bucket samples by the asset's lifecycle phase at the label's decision time and fit a separate time-to-collapse survival curve per phase (plus per-phase time-to-peak), so an ignition-phase asset decays on a faster curve than a seeding-phase one. Prediction must select the asset's current-phase fit (`details.hazard_phase`) and fall back to the global aggregate for phases with no learnable curve. Per-phase curves must persist as `forecast.hazard.{phase}.mean_hours_to_collapse` / `mean_hours_to_peak` metrics.
- With insufficient samples the engine must train nothing, write no forecasts, and report `component:forecast` health as `yellow`.
- `collapse_probability_24h` must feed the risk engine (>= 0.6 adds risk points) and `ExitRiskScore`.
- Each persisted forecast must include row-level `feature_contributions` for the velocity features (`kol_velocity`, `github_star_velocity`, `hf_download_velocity`) and the other forecast inputs. Each contribution records the current value, neutral/missing baseline, and local ignition/collapse probability deltas; `/forecasts` and the Forecast UI must surface these impacts, including missing evidence honestly.

## Forecast Drift Proof

- Each training run splits the test set into a trailing window (`FORECAST_DRIFT_TRAILING_HOURS`) and an older baseline, and computes precision-at-10 and calibration error on each.
- Trailing performance worse than the baseline by `FORECAST_DRIFT_PRECISION_MARGIN` (and below `FORECAST_DRIFT_MIN_PRECISION`) or worse than the persisted historical metrics by `FORECAST_DRIFT_PRECISION_FRACTION` must report `component:forecast_drift` health as `yellow`; trailing precision below `FORECAST_DRIFT_SEVERE_PRECISION` reports `red`.
- Trailing calibration error above the baseline or historical value by `FORECAST_DRIFT_CAL_MARGIN` (and above `FORECAST_DRIFT_MAX_CAL_ERROR`) must also flag drift.
- A trailing window with fewer than `FORECAST_DRIFT_MIN_SAMPLES` samples reports `yellow` with `drift=insufficient_trailing` instead of a false alarm.
- Drift measures (`trailing_precision_at_10`, `baseline_precision_at_10`, `historical_precision_at_10`, `trailing_calibration_error`, and a numeric `drift.status`) must be persisted as `forecast.drift.*` backtest metrics on the same run.

## Calibration-Bias Guard Proof

- Each completed training run must compute the gap `real-only calibration error − blended calibration error` across the TEST set. A positive gap past `FORECAST_CAL_GAP_THRESHOLD` (default 0.10) with at least `FORECAST_CAL_GAP_MIN_SAMPLES` real observed test samples reports `component:forecast_calibration` health as `red`; otherwise `ok` (and `n/a` when too few real samples) — surfaced in Feed Health.
- Over threshold, the notifier must push a **Calibration Bias Warning** (priority 4, `warning` tag) with the gap, blended vs real-only calibration errors, and real-sample count, once per `FORECAST_CAL_GAP_COOLDOWN_HOURS` (deduped against the last red row, evaluated before writing the current run's marker so it cannot suppress itself).
- The check must run as part of the shared training path (`ForecastEngine.run`), so the scheduler profile, the zero-container worker loop, and the `python -m engine` loop all get it for free; a disabled ntfy must record health without pushing.

## Real-Only Usage Gate Proof

- With `FORECAST_GATE_ON_REAL_METRICS` enabled, `ForecastEngine.run` must judge the trained model by its **real-only** test readout, never the blended one. When `forecast.real_test_samples` < `FORECAST_GATE_MIN_REAL_SAMPLES` (default 5) or the real-only calibration error > `FORECAST_GATE_MAX_REAL_CAL_ERROR` (default 0.25), it must emit **no** production forecasts and report `component:forecast` health as `yellow` with the reason, returning `status="gated"` and the real-only readout.
- When trusted (enough real samples and a healthy real-only calibration), behavior is unchanged. The gate is off by default — enabling it can stop forecast production until the real-only metrics recover, so it is an explicit operator decision.

## Push Notifier Proof

- Alert generation applies the configurable signal-quality gate (`ALERT_QUALITY_NOISE_FLOOR` after `ALERT_QUALITY_MIN_RATINGS` rated ACKs) consistently across scoring, radar, fingerprint, lifecycle, prelaunch, and catalyst alerts; a quieted family is explicitly resumed with `POST /alerts/types/{alert_type}/reenable`.
- Operators must be able to **ACK open alerts**: `POST /alerts/{id}/ack` must set `state=acked` with `acked_at` and an optional `ack_quality` (`useful`/`noise`), be idempotent on re-ack (updating the rating), reject invalid quality (422), and 404 on a missing alert. An ACKed alert must leave the notifier's open set — `NTFY` flushes must not push it (repeat-push suppression) — and, because alert creation dedupes on event ref regardless of state, later scans must not re-create it.
- `GET /alerts/quality` must return the **signal-quality ledger**: total ACKed, useful/noise/unrated counts, the useful rate, and the most recently ACKed alerts with their ratings, so the operator can see which alert types earn their keep.
- With `NTFY_ENABLED` and `NTFY_TOPIC` set, each new open alert of a configured type (`NTFY_ALERT_TYPES_CSV`) within the `NTFY_BACKLOG_HOURS` window must POST to ntfy.sh exactly once and be marked `notified_at`.
- The lifecycle state machine must create one idempotent `lifecycle_transition` alert (ref `lifecycle:{event_id}`) the moment an asset reaches a terminal danger phase — COLLAPSE, RUGGED, or DEAD — and never for non-terminal phases (IGNITION etc.) or SURVIVOR. The notifier must push it at priority 5 with the `collision` tag, exactly once.
- A second flush must not re-push already-notified alerts.
- With `NTFY_DAILY_DIGEST_ENABLED`, one UTC-day digest must summarize every terminal lifecycle transition and ignition event in the preceding 24 hours, persist its delivery state, and not duplicate on repeated scans. A failed digest must remain retryable.
- A failed push must leave `notified_at` NULL so the alert is retried on the next scan, and must report `component:notifier` health as `yellow`.
- Alerts older than the backlog window must not be pushed, so re-enabling the notifier cannot dump a stale backlog.
- Without a topic the notifier must be skipped entirely.

## Phase 4: Zero-Container Self-Hosting Validation Runbook ($0 Stack)

The full stack runs on one machine with **no containers** (SQLite + local
Parquet). This runbook proves each layer works before it is trusted.

### 4.1 Bootstrap the zero-container profile

```bash
python scripts/bootstrap_local.py
```

Must print a ready banner referencing `sqlite:///serpent.db`, `local` archive
backend, and the `data/archive` directory. Verify on disk:

- `serpent.db` exists and is a SQLite file (`file serpent.db`).
- `data/archive/` exists.
- `python -c "import sqlite3; c=sqlite3.connect('serpent.db'); print(c.execute('select count(*) from sources').fetchone())"` returns a non-zero source count.

### 4.2 Single scan end-to-end

```bash
ENV=local-single python -m ingestion.worker --once
```

Must complete without a stack trace. Verify in SQLite:

- `select component, state from system_health order by ts desc limit 10;` shows
  `worker=ok` and per-source rows (`source:dexscreener_profiles`, `archive`, ...).
- `select count(*) from assets;` > 0 when live sources are reachable (a fully
  offline machine instead shows `source:*` health rows in `red` with the exact
  reason — degraded, never fake data).

### 4.3 Parquet compaction + retention

```bash
ENV=local-single python -m ops.archive --once
```

Verify with the local lake (this is the proof that free endpoints dying never
costs history):

```bash
find data/archive -name '*.parquet' | head          # partitions exist
ENV=local-single python -m ops.archive --query "SELECT source_type, count(*) AS n FROM evidence GROUP BY 1 ORDER BY n DESC"
```

Must return rows with real counts (not an empty list) when raw evidence exists.
The archive health row (`component=archive`) must be `ok` with
`compacted/partitions/pruned` counts.

Idempotency: re-running `--once` must not grow the lake (manifests are keyed by
`object_key`) and must report `compacted=0` once everything is archived.

## Retention Autopilot Proof (Phase 4)

- `python -m ops.retention --once` must run the full retention pass —
  compaction + pruning + lake report — and persist a `RetentionRun` row with
  the Parquet lake totals (`partitions`, `archived_rows`, `byte_size`),
  `compacted`/`pruned` counts, and `growth_bytes`/`growth_pct` vs the previous
  pass (first pass reports the whole lake as growth, `growth_pct` NULL).
- Compaction must run on a **per-partition schedule**: each pass computes the
  `(source_id, year, month)` partitions whose unarchived evidence is older
  than `ARCHIVE_COMPACT_AFTER_HOURS` and compacts exactly those; a pass with
  nothing due does zero compaction work (`compacted=0`, `partitions=0`) while
  pruning still runs. The retention autopilot — not the ingestion worker — is
  the sole owner of compaction: a scan must report `archive=0` (skipped) and
  write no Parquet files, so the cadence fully owns the archive.
- Each pass must record `component:lake` health: `ok` with the growth in the
  message (`partitions=.. rows=.. bytes=.. growth_bytes=.. growth_pct=..%`),
  `red` with the error when compaction fails (and no `RetentionRun` is
  persisted). The row appears in **Feed Health** automatically via `/health`.
- `python -m ops.retention --check-due` must exit 0 when the cadence
  (`RETENTION_CADENCE_HOURS`) since the last pass has elapsed, 1 otherwise;
  a disabled autopilot is never due, and no recorded pass is due.
- The worker must run a **pre-scan lake freshness check** using the same gate
  as `--check-due`: when a pass has been recorded but the cadence since it has
  elapsed, it must record `component:lake` health as `yellow` (`lake stale:`
  with the hours since the last pass and the cadence) so **Feed Health** flags
  the stale lake before the scan runs. Fresh lakes, lakes with no pass yet,
  and disabled autopilots record nothing.
- The pass must evaluate the **retention budget**: projecting the lake growth
  (same linear fit as `GET /retention/growth`) against
  `ARCHIVE_LAKE_MAX_BYTES`, a projected fill within
  `RETENTION_BUDGET_ALERT_DAYS` (default 14) must record
  `component:lake_budget` health (`yellow`, or `red` at/over capacity) and
  push one ntfy warning (`Lake Budget Warning`, priority 4; `Lake Full`,
  priority 5, when at/over capacity) with the days-to-full, fill percentage,
  growth rate, and cap. Repeated passes must refresh the health row but not
  re-push within `RETENTION_BUDGET_ALERT_COOLDOWN_HOURS` (default 24); a
  horizon beyond the alert window, or no usable growth trend, records
  nothing.
- The scheduler must drive the pass on the cadence: the APScheduler entrypoint
  (`python -m ingestion.scheduler`) registers a `retention_autopilot` job, and
  the worker loop (`python -m ingestion.worker --loop`) checks `retention_due`
  after each scan. Standalone boxes use the OS scheduler:
  `deploy/systemd/serpent-retention.timer` + `.service` (Linux) or
  `scripts/install_retention_task.ps1` (Windows Task Scheduler), or run one
  pass by hand with `make retention` (`python -m ops.retention --once`).
- `GET /retention/growth` must return the retention-pass history plus a
  projected disk-full horizon: a linear regression of `byte_size` over elapsed
  time extrapolated to `ARCHIVE_LAKE_MAX_BYTES` (default 100 GiB). With fewer
  than two passes or a flat/shrinking trend the projection is `null`; the
  response also carries the current fill percentage (`pct_full`). The
  **Archive & Retention** view renders the trendline chart with the projected
  extension and capacity cap.

### 4.4 Full-rig parity (MinIO backend)

With `ARCHIVE_BACKEND=s3` (docker profile) the same jobs run against MinIO:

```bash
python -m ops.archive --once
python -m ops.archive --query "SELECT partition_year, count(*) AS n FROM evidence GROUP BY 1"
```

Inspect `GET /archive/manifests` (API) or the **Archive & Retention** UI view.

CI must run both archive modes: the `moto-s3` job executes
`tests/test_archive_s3.py` without a live service, while the
`docker-minio-parity` job starts the Docker profile's real MinIO/Postgres/
Redis services, runs migrations and fixture seeding, executes a live
`ops.archive --once` smoke, and runs `ops.parity --once` against the S3 lake.
The S3 backend must also be covered **without a live MinIO container** via
moto (`tests/test_archive_s3.py`): `object_exists` (missing vs present),
`list_objects` (prefix narrowing, parquet-only, missing prefix), `download_to`
materialization, `make_store` backend selection, and the full
compaction→`query_archive` DuckDB path (partitioned Parquet written to the
mock bucket, then queried back with `SELECT ... FROM evidence`). A second
compaction batch landing in the same `(source, year, month)` S3 partition
must merge into the existing partition object (manifest `row_count` grows,
one object in the bucket, `query_archive` returns both batches) — the moto
mirror of the local-store merge test, proving the compactor never clobbers
lake history on MinIO either.

### 4.5 Zero-cost audit

The stack must not require any paid key or subscription:

- No `HELIUS_API_KEY`, `ALCHEMY_API_KEY`, `ETHERSCAN_API_KEY`, or
  `REDDIT_CLIENT_*` values are required for the default path.
- Every paid key in `.env` is an enhancement; the engine must run and score
  with them empty.
- Free endpoints (DexScreener, GeckoTerminal, public RPCs, Reddit JSON, RSS,
  ntfy.sh) are the only network dependencies; failures degrade to `health`
  states with reasons, never fabricated rows.

### 4.6 Push + UI smoke

- Optional: `NTFY_ENABLED=True NTFY_TOPIC=<unique> ENV=local-single python -m ingestion.worker --once`
  pushes to https://ntfy.sh/<unique> on a t0 alert.
- `uvicorn api.main:app --host 0.0.0.0 --port 8000` serves `/health`.
- `streamlit run ui/app.py --server.port=8501` renders the dashboard incl. the
  **Archive & Retention** view.

## Lifecycle State Machine Proof

- Without a tradable pool the phase must resolve to `seeding`; a book emptied
  by withdrawals resolves to `rugged`; no trades for over a week resolves to
  `dead`; a one-hour crash below the collapse threshold or any liquidity
  withdrawal resolves to `collapse`; ignition events or a young pool resolve to
  `ignition`; volume acceleration with a strong buy/sell ratio resolves to
  `parabolic`; sell-pressure dominance or negative holder growth while price
  holds resolves to `saturation`.
- The scan must persist exactly one idempotent `lifecycle_events` row per
  transition (re-scanning the same evidence emits nothing), transitions may
  only advance along SEEDING → IGNITION → PARABOLIC → SATURATION → COLLAPSE
  (or jump to a terminal exit), and `component:lifecycle` health must report
  scan state.
- `lifecycle_phase` must appear as a feature (0=seeding .. 4=collapse) and
  feed the risk engine: collapse phase adds risk points/reasons and lifts
  `ExitRiskScore`.
- `GET /lifecycle/current` and `GET /lifecycle/events` must return rows with
  symbol, chain, phase, ts, and confidence; `GET /lifecycle/alerts` must return
  terminal `lifecycle_transition` alerts joined to their event evidence; the
  Lifecycle Radar UI view must render current phases, transitions, and inline
  terminal evidence.

## Backtest Lead-Time Proof

- Every `BacktestRun` must record `git_sha` (or `NULL` outside a git checkout).
- Each run must compute `median_ignition_lead_minutes` (flag → pump start),
  `median_collapse_warning_lead_minutes` (flag → -70%), and
  `false_alarm_rate` (alerts that did not collapse within the forward window).
- `python -m backtest.runner --start ...` must run a replay-safe backtest and
  print run id, status, git sha, and metrics.
- Each scoring `BacktestRun` must also surface the latest forecast training
  run's blended **and** real-only metrics (`forecast.precision_at_10`,
  `forecast.calibration_error`, `forecast.precision_at_10_real`,
  `forecast.calibration_error_real`, `forecast.real_test_samples`,
  `forecast.test_samples`), so walk-forward output reports both readings next to
  the scoring metrics and the operator can see the real-only numbers that
  dense-label interpolation could otherwise mask.

## Lifecycle Walk-Forward Backtest Proof (pump_physics §9)

- `python -m pump_physics.backtest --start ... --step-hours 6 --forward-hours 48`
  must replay the lifecycle scanner step-by-step over persisted evidence only
  (`observed_at <= decision_ts`), maintaining the monotonic phase in memory.
- Every transition into IGNITION / PARABOLIC must be scored against realized
  forward prices (crossing `+20%` within the window ⇒ true positive with a lead
  time; otherwise false alarm), and every SATURATION / COLLAPSE transition
  against `-70%` — the same thresholds the rule-based runner uses.
- Per-type metrics must persist on a `BacktestRun` with
  `model_version = lifecycle_model_version`: `lifecycle.ignition.*`,
  `lifecycle.collapse.*`, `lifecycle.overall.*` (precision, false alarm rate,
  median lead minutes), `lifecycle.assets_with_transitions`, and
  `lifecycle.decision_steps`.
- A transition with no entry price at decision time must be counted as
  unevaluated, never as a false alarm.

## Forecast Time-to-Peak Proof

- The forecast artifact must include `expected_hours_to_peak` alongside
  `expected_hours_to_collapse`, derived from a discrete hazard fit on ignition
  samples; it must be persisted on `Forecast` rows and exposed via
  `/forecasts` and the Forecast UI view.

## Parquet Archive Proof

- Raw evidence older than `ARCHIVE_COMPACT_AFTER_HOURS` and not yet archived
  must be written as partitioned Parquet `source=…/year=…/month=…/part-0.parquet`
  under `ARCHIVE_PREFIX`, with one idempotent `ArchiveManifest` per `object_key`.
- Re-running compaction must not duplicate partitions or manifests; archived
  rows are marked `archived_at` and skipped on the next run.
- Rows older than `ARCHIVE_RETENTION_DAYS` that are **not** referenced by
  normalized tables (snapshots, flags, news) must be pruned from the hot DB;
  referenced provenance rows must survive.
- `query_archive()` must expose the lake as a `evidence` view to DuckDB and
  return real aggregates (`SELECT count(*) …`) for both local and S3 stores.
- `component:archive` health must report `ok` after a successful run and `red`
  with the reason when the store fails.

## Contract Analysis Persistence Proof

- Each analyzed asset's findings (honeypot patterns, mint authority, pause
  function, ownership not renounced, rug deployer) must be persisted as
  point-in-time `contract_analysis` raw evidence (the `findings` list is the
  single source of truth) plus one evidence-backed `ContractFlag` row per
  finding — so `suspicious_contract_flags` counts the FULL flag set and the
  lake replay reconstructs it from the archived evidence, not just
  low-liquidity scans.
- Re-analysis of the same asset must be idempotent: identical evidence
  dedupes on content hash and an existing `(contract, flag_type)` flag row is
  never duplicated.

## DuckDB Lake Feature Parity Proof (features §6)

- `features/lake.py` must reconstruct the normalized market/liquidity series
  from the archived Parquet lake via DuckDB — unnesting the GeckoTerminal
  `new_pools` evidence payloads, resolving the `h1|h24|m5` window precedence,
  flooring `observed_at` to the hour, and deduping first-wins per
  `(pair, hour)` — and feed the same `compute_market_block` math the SQL path
  uses.
- The parity test (`tests/test_lake_features.py`) must seed identical
  observations into the SQL tables AND the lake, then assert every
  lake-covered feature (`LAKE_FEATURE_NAMES`, the 11 market/liquidity names
  plus the 4 on-chain holder/contract-flag names) matches between
  `FeatureFactory.build_for_asset` (SQL) and
  `LakeFeatureFactory.build_for_asset` (DuckDB) — values *and* missing flags.
- The lake path must reconstruct the on-chain holder features
  (`holder_count`, `holder_growth`, `top_holder_concentration`) from the
  archived `chain_rpc` holder-snapshot evidence (`solana_rpc` payloads with
  `mint`/`supply`/`largest_accounts`, deduping per `(hour, wallet)` exactly
  like `insert_holder_once`, with the same latest-snapshot count, top-10
  concentration, and one-hour-ago growth delta the SQL path computes). The
  reconstruction is **chain-agnostic**: an EVM asset whose holder evidence
  arrives on the `evm_holders` source (Blockscout v2, `base.blockscout.com`
  / `eth.blockscout.com`, no key) with the same payload shape must
  reconstruct identically — a parity test seeds Base-chain holder evidence
  and asserts the lake matches the SQL path.
- `suspicious_contract_flags` must reconstruct as the FULL count of
  evidence-backed contract flags: the GeckoTerminal pool scans whose
  `reserve_in_usd` fell below `min_discovery_liquidity_usd` (one
  `low_liquidity` flag each) PLUS the contract analyzer's findings —
  honeypot patterns, mint authority, pause function, ownership not
  renounced, rug deployer — archived as `contract_analysis` evidence with a
  deterministic `findings` list. Both paths apply the SQL severity filter
  (`warning | high | critical | black`) so they count the same
  `ContractFlag` rows; like the SQL path, the feature must report `0.0`
  (never missing) when no flags exist.
- An empty lake must report the market block as honest `missing`, never
  fabricated zeros; evidence for other base addresses must be ignored.
- `build_and_persist_features(..., feature_source="lake")` must replay the
  lake-covered block entirely from the archived Parquet (no live
  market/liquidity tables touched) and persist `Feature` rows through the same
  `upsert_feature` as the SQL path; `"sql"` (default) keeps the live-table
  behavior, and any other value must raise `ValueError`. `ScoringEngine` and
  `BacktestConfig`/`run_backtest`/`python -m backtest.runner --feature-source
  lake` must thread the switch through, and the run's `config_json` must
  record which source produced its features.
- The parity test must also run the **full pipeline** through
  `build_and_persist_features` on the same seeded fixture: persisting with
  `feature_source='sql'` and then `'lake'` must yield IDENTICAL persisted
  `Feature` rows for all 15 lake-covered names — `feature_value`, missing
  flag, `source_count`, and `freshness_score` — not just matching in-memory
  values, so the write path (not only the read path) is provably
  interchangeable.
- Lake timestamps must be written as naive UTC (`TIMESTAMP`, not `TIMESTAMP
  WITH TIME ZONE`) so DuckDB truncation/comparison needs no optional `pytz`.
- Reconstructions must be cached at the class level on `LakeFeatureFactory`
  keyed by `(store, asset address, hour)` (a bounded LRU shared across
  factory instances), so repeated backtest steps over the same window never
  re-download the Parquet or re-run DuckDB for an already-served `(asset,
  hour)` bucket. The cache is exact for hour-boundary decisions (the
  backtest contract) and bypassed for sub-hour decision times; a
  `clear_cache()` classmethod drops it (e.g. after new evidence is compacted
  into the lake). The retention autopilot must call `clear_cache()` whenever
  a pass actually compacts new Parquet (`compacted > 0`), so long-lived
  worker/API processes invalidate stale reconstructions; a pass with nothing
  due keeps the cache warm.
  due keeps the cache warm. The cache must stay consistent under concurrent
  access: simultaneous threads building the same `(asset, hour)` must all
  return identical reconstructions, leave exactly one cache entry per key,
  and keep `hits + misses == lookups` (a regression test races 8 threads on
  the single-asset and batched paths).
- `LakeFeatureFactory` must expose process-lifetime cache hit/miss counters
  (`cache_stats()` / `reset_cache_stats()`), and a `feature_source="lake"`
  backtest run must snapshot them around the walk-forward and persist the
  delta as `lake_cache.hits`, `lake_cache.misses`, and
  `lake_cache.saved_queries` (`hits == saved_queries`) metrics — so replay
  runs report how many DuckDB queries the cache saved (a warm second run
  over the same window serves everything from cache: hits ≥ 1, misses 0).
- `LakeFeatureFactory.build_for_assets` (and `persist_for_assets`) must
  reconstruct **all assets in one pass**: the Parquet lake is listed and
  downloaded once and every asset is queried in a single DuckDB session
  (the reconstruction SQL filters by `LIST_CONTAINS($asset_addresses, ...)`
  and the results are grouped per asset), so a full-lake job like the
  parity CI is O(1) downloads, not O(assets).

## Lake-vs-SQL Parity CI Proof

- `python -m ops.parity --once` must run the same comparison as the parity
  test against the *production* lake: for every asset (or the first
  `PARITY_MAX_ASSETS`), build the lake-covered features through both
  `FeatureFactory.build_for_asset` (live SQL tables) and
  `LakeFeatureFactory.build_for_asset` (DuckDB over the archived Parquet) at
  a floored-hour decision time, and report any divergence among
  `LAKE_FEATURE_NAMES` (value beyond `PARITY_TOLERANCE` or a missing-flag
  split) as a mismatch.
- The comparison decision time must be clamped so every piece of evidence at
  that time is provably archived: at least `PARITY_COMPARE_HOURS_AGO` in the
  past AND older than `ARCHIVE_COMPACT_AFTER_HOURS` +
  `RETENTION_CADENCE_HOURS`, floored to the hour (the lake cache's exactness
  contract).
- Each run must record `component:parity` health: `ok` with zero mismatches,
  `red` (or `yellow` below the threshold) with mismatches, carrying the
  count, compared assets, and decision time in the message.
- A mismatch at/above `PARITY_ALERT_THRESHOLD` must page via ntfy
  (`Serpent Circle - Lake Parity Mismatch`, priority 4) with the first few
  `SYMBOL [feature]: sql=... lake=...` divergences, at most once per
  `PARITY_ALERT_COOLDOWN_HOURS` (the last red health row is the cooldown
  marker, evaluated *before* this run's row is written).
- The job must run on the daily cadence: `ingestion/scheduler.py` registers
  it on `PARITY_FREQUENCY_HOURS` (docker profile) and the zero-container
  worker loop runs the cadence-gated `maybe_run_parity()` after each scan
  (the last `component:parity` row is the run marker, so the first run is
  due and subsequent runs wait out the frequency). `make parity` / `parity`
  in the dev scripts run one pass by hand; `--strict` exits non-zero on a
  mismatch for CI integration.
- A failed run must never kill the caller: it records `component:parity`
  health as `red` and returns `{"error": ...}`, and a single asset's
  comparison failure counts as an error instead of aborting the pass.
- `GET /parity/latest` must return the most recent parity run's structured
  summary — state, mismatch count, compared assets, decision time (parsed
  from the health-row message), tolerance, compare horizon, and error
  count — or 404 before any run has completed, and the **Feed Health** view
  must render it in a Lake-vs-SQL Parity panel.
- Each run must persist every divergence as a `parity_mismatches` history
  row (run timestamp, comparison decision hour, asset, symbol, feature,
  SQL and lake values, missing flags, run state), pruning rows older than
  `PARITY_HISTORY_RETENTION_DAYS` (default 90) at the start of each pass;
  a clean run must leave no rows. `GET /parity/mismatches` must return the
  history newest-run-first with optional `asset_id` / `feature` filters, and
  the Feed Health parity panel must render it in a Divergence history
  expander.

## Zero-Container Profile Proof

- With `ENV=local-single` and no explicit `DATABASE_URL`, settings must resolve
  to `sqlite:///serpent.db`; with no explicit `ARCHIVE_BACKEND`, the archive
  backend must resolve to `local`. Explicit environment values must always win.
- `scripts/bootstrap_local.py` must create the SQLite file, seed
  chains/sources/venues, create `ARCHIVE_LOCAL_DIR`, and print ready
  instructions without ever starting a container.
- The ingestion worker, API, UI, radar/fingerprint/forecast pipeline, and the
  archive job must all run against the SQLite profile (the full test suite
  already runs on an in-memory SQLite engine).

## Chain-Native Holder Proof

- Solana holder ingestion must write idempotent `holders` rows from RPC token-account evidence before scoring runs.
- `source:solana_holders` health must report the number of holder rows and assets scanned, or preserve the exact RPC failure reason when public RPC/provider limits block the scan.
- Base/Ethereum holder ingestion must write idempotent `holders` rows from free public Blockscout v2 token-holder evidence (`EVMHolderClient`, no key): token info for `total_supply` plus the token-holders page, normalized to the same `mint`/`supply`/`largest_accounts` evidence shape the Solana path and the lake replay parse. The scan is bounded by `EVM_HOLDER_SCAN_LIMIT` per chain and paced by `EVM_HOLDER_RPC_PAUSE_SECONDS` to stay under Blockscout's unauthenticated rate limit (~3 req/min). One aggregate `source:evm_holders` health row must report the holder rows and scanned-chain count (`yellow` with `0 eligible assets` when chains exist but nothing is scannable), and the whole scan must be idempotent on repeat runs.
