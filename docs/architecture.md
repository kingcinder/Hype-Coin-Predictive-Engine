# Architecture

The MVP is a modular monorepo with replaceable subsystems and a single Python runtime image. Service boundaries are entrypoints first, not separate codebases.

## Ownership Map

- `ingestion` owns source clients, retries, idempotent writes, watermarks, and feed health.
- `radar` owns t0 ignition detection (first liquidity injections, sniper bursts), the on-chain liquidity withdrawal watcher, and the prelaunch queue, all derived from persisted point-in-time evidence.
- `mempool` owns the sub-minute t0 watchers: Solana signature polling (poor man's geyser) and EVM factory `PairCreated` log discovery.
- `narrative` owns the free public crawlers (Reddit, YouTube RSS, GitHub, HuggingFace, RSS, and public Telegram channels via Telethon) and the hand-built minhash narrative clustering.
- `catalyst` owns rule-based catalyst extraction from news and the upcoming-catalyst timetable alerts.
- `fingerprint` owns wallet syndicate learning (co-occurrence clusters) and per-asset recidivism assessments.
- `pump_physics` owns the hype-lifecycle state machine: deterministic phase detection (SEEDING → IGNITION → PARABOLIC → SATURATION → COLLAPSE, exits DEAD/RUGGED/SURVIVOR) from persisted point-in-time evidence, idempotent `lifecycle_events` transitions, and the `lifecycle_phase` feature that feeds risk scoring.
- `forecast` owns point-in-time label generation, purged train/test gradient boosting with isotonic calibration, hand-built discrete hazard models for time-to-peak and time-to-collapse, and drift metrics.
- `ops` owns the ntfy.sh push notifier (ignition/withdrawal/recidivism/lifecycle-terminal phone alerts — `lifecycle_transition` fires the moment a token reaches COLLAPSE/RUGGED/DEAD — idempotent via `alerts.notified_at` — plus operational RPC-pool and lake-budget warnings) and the Parquet archive: raw evidence is compacted into `source/year/month` partitioned Parquet over MinIO or local disk, marked `archived_at`, pruned past `ARCHIVE_RETENTION_DAYS`, and queried through DuckDB (`python -m ops.archive --query`).
- `env=local-single` is the zero-container profile: SQLite instead of Postgres, in-process scheduling instead of Redis, and the local-disk archive backend instead of MinIO. `scripts/bootstrap_local.py` bootstraps it with no containers.
- `storage` owns schema, migrations, sessions, seed data, and point-in-time persistence contracts.
- `features` owns feature production from persisted evidence only.
- `risk_engine` owns hard rejects and traffic-light risk states.
- `scoring` owns score formulas, explanation records, and alert generation.
- `api` owns typed read endpoints for the UI and external inspection.
- `ui` owns the operator-facing research terminal.
- `backtest` owns replay-safe historical scans and metrics.

## Read-path batching

The score scan is the engine's hot read path: every iteration builds features
for every known asset and scores each. Both read paths batch by design —
invariant data is loaded once per scan, never per asset:

- The **SQL path** (`features/factory.py`, `scoring/engine.py`) loads the
  source id→name map once per `persist_for_assets` scan and threads it through
  every per-asset build (velocity features no longer re-scan the whole
  `sources` table per asset), and `_build_price_updates` / the LLM pass load
  `Asset`/`Chain` rows with `assets_for_ids` + one `IN` query instead of
  `session.get` per asset.
- The **lake path** (`features/lake.py`) already reads the whole asset batch
  from the Parquet lake in one DuckDB pass and caches reconstructions keyed by
  `(store, asset, hour)`.

Both paths feed the identical shared math (`compute_market_block`), so the
feature values are path-independent by construction.

## Data Flow

1. Ingestion fetches source data and writes raw evidence plus normalized rows.
2. Mempool watches new mints/pools (Solana signatures, EVM factory logs) and seeds point-in-time rows.
3. Prelaunch queue ranks tokens before their pool exists.
4. Narrative crawlers persist mentions/news; the clusterer groups them into narrative clusters.
5. Catalyst extractor turns news into scheduled catalysts and alerts.
6. Radar detects ignition events (first liquidity injection, sniper burst, liquidity withdrawal) from persisted evidence.
7. Fingerprint learns wallet syndicates from holder/deployer co-occurrence and scores recidivism per asset.
8. Forecast generates labels, trains calibrated phase-transition models, and writes per-asset forecasts.
9. Feature factory reads only rows where `observed_at <= decision_time`.
10. Risk engine produces hard-reject state and reasons, including withdrawal, recidivism, and forecast-collapse penalties.
11. Pump physics advances each asset's lifecycle state machine and persists idempotent transition events.
12. Scoring writes ten separated scores plus explanation records.
13. API/UI present ranks, changes, risks, evidence, ignition events, lifecycle phases, forecasts, and feed health.
14. Retention autopilot compacts raw evidence older than `ARCHIVE_COMPACT_AFTER_HOURS` into partitioned Parquet (MinIO or local disk) on a per-partition schedule owned by its cadence (`RETENTION_CADENCE_HOURS`) — the ingestion worker never touches the archive. Each pass marks rows archived and prunes unreferenced rows past the retention window; the lake is queryable with DuckDB.
15. Backtest replays historical decision times with the same point-in-time rule and records `git_sha` plus lead-time / false-alarm metrics.

## Truth Rules

- Market history is append-only or natural-key idempotent.
- Missing fields increase uncertainty instead of becoming fake zeros.
- Lower-trust sources cannot override chain, venue-native, or verified official evidence.
- `BLACK` risk overrides every positive hype or research score.

