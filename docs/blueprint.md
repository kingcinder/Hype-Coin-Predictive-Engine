# Serpent Circle — Megalithic Blueprint v2

**Goal:** predict, with honest confidence intervals, which emergent tokens are about to
*ignite* (spike hard right after launch/ICO) and which ignited tokens are about to
*collapse* (fall fast, hard, and without stopping). Everything runs locally, modules are
built by hand, and the operating cost is **$0/month** (or ~$5/month on a VPS if you want
24/7 uptime).

This is not a trading bot, not a wallet, not guaranteed profit. It is a local-first
research intelligence terminal that produces *predictions with calibration*, so the
operator makes the final call.

---

## 1. The Thesis: hype is a physics problem, not a price problem

Every hype-coin arc — ICO, fair launch, pump.fun raid, AI-token debut — follows the same
five-phase lifecycle:

```
SEEDING -> IGNITION -> PARABOLIC -> SATURATION -> COLLAPSE
```

The market prices these phases with a **delay**. Price does not lead hype; hype leads
price, then liquidity follows, then price. The engine already measures the present
(HypeScore, RiskScore). The blueprint's job is to predict the **phase transitions**:

1. **Pre-launch:** token exists as a contract / project page / social seed, but has no
   tradable pool yet. → *Predict which will ignite within the next hours/days.*
2. **Ignition:** first liquidity injection, first sniper buys, first shill wave. → *Flag
   within minutes of t0, not after the pump.*
3. **Collapse onset:** sell pressure inverts, LP is withdrawn, holder growth stalls,
   unlocks hit. → *Emit a calibrated "exit" warning while exits are still possible.*

**The core crafty insight:** you do not predict the price. You predict (a) *intent* and
(b) *phase transitions* — both of which leave observable, cheap, public traces before
price moves:

- Intent is manufactured on Telegram/Reddit/YouTube/GitHub **before** liquidity lands.
- Liquidity is *added* on-chain **before** price pumps.
- Bots snipe **before** humans can buy.
- LP is *removed* and wallets *move* **before** the dump.

None of these require a paid API. All of them are public data. The architecture below is
a set of hand-built modules that collect those traces, fuse them, and score transitions.

---

## 2. The Zero-Cost Data Layer — paid services replaced by hand-built modules

Every paid/limited service the typical hype-coin stack uses has a quiet, superior,
local substitute. Build these; never subscribe.

| Paid / limited service | Typical cost | Quiet superior local substitute |
|---|---|---|
| Helius / QuickNode / Alchemy (RPC) | $49–$500/mo | Free public RPC rotation (Solana mainnet, Ankr, drpc, publicnode, base public RPC) + WebSocket log streams + optional self-run node on old hardware/VPS |
| Etherscan / Basescan API | $29+/mo | **Blockscout public instances are free with no key** (`base.blockscout.com`, `ethereum.blockscout.com`). Also self-hosted Blockscout (Phase 4) |
| Moralis / Shyft (indexers) | $50+/mo | Solana RPC `getSignaturesForAddress` polling + log parsing — free, no key |
| CoinGecko API | $29+/mo (paid tiers) | GeckoTerminal free API + DexScreener free API (already in MVP) + self-built snapshots stored forever in Postgres/Parquet |
| LunarCrush / Brandwatch / social intel | $99–$1000/mo | **Narrative Radar module**: Reddit public JSON, RSSHub + Nitter bridges, YouTube RSS feeds, public Telegram channels, GitHub search API, HuggingFace trending — all free |
| OpenAI / Anthropic (sentiment) | usage-based | Local LLM (Ollama, Qwen/llama 3.2 8B) for classification, or **no LLM at all**: lexicon + locally-trained fastText/sentence-transformers embeddings on your own corpus |
| TradingView alerts / paid scanners | $15–$60/mo | The engine's own scanner + alert table + **ntfy.sh** push (free, no account) |
| Glassnode / Kaiko (metrics) | $29–$790/mo | Derive on-chain metrics from the RPC data you already fetch |
| Tatum / data aggregators | $149+/mo | Direct RPC + explorer calls, cached and watermarked locally |
| CoinMarketCap API | $69+/mo | DexScreener + GeckoTerminal + self-maintained listings table |
| Pushover / paid push | $5/mo | **ntfy.sh** (free) or self-hosted **Gotify** |
| Hosted cron / scheduler | $5–$20/mo | APScheduler in-process (already present) or systemd timers / Windows Task Scheduler |

**Rules that keep the free layer honest:**

- Every public endpoint gets a **token-bucket rate limiter**, exponential backoff, and an
  `IngestionWatermark` — the MVP already has this discipline; extend it to every new module.
- Every response is **snapshotted to MinIO (Parquet) + normalized rows** so a dead free
  endpoint never destroys history. Free endpoints die; your local archive is the
  moat.
- Free RPC endpoints rotate: a **curated endpoint pool per chain** with health
  scores and automatic failover is built (`ingestion/rpc_pool.py` — one pool each
  for solana/base/ethereum from `*_RPC_POOL_CSV`, health decays on failure,
  `RPC_POOL_FAILURE_THRESHOLD` consecutive failures take an endpoint down, `pick()`
  prefers the healthiest endpoint with LRU tiebreak, and a background daemon probe
  thread per chain (`RPC_POOL_BACKGROUND_PROBE_ENABLED`, `RPC_POOL_PROBE_INTERVAL_SECONDS`)
  plus a per-scan probe pass health-check downed endpoints and jump-start them back
  to `RPC_POOL_RECOVERY_HEALTH` on success — no waiting for traffic; EVM watcher
  and Solana client requests feed each chain's health, and endpoint states surface
  as `component:rpc_pool:{chain}` health rows).
- "Free" must never mean "sneaky against ToS." The craft is in using *public JSON
  endpoints, RSS bridges, and on-chain public data* — not in scraping behind logins or
  hammering endpoints. Respect rate limits and robots.txt; prefer the endpoint the
  service's own frontend uses.

---

## 3. Module Map — what exists vs. what to build

### Existing (keep and harden)

| Module | Role |
|---|---|
| `ingestion` | Source clients, retries, watermarks, normalization, feed health |
| `storage` | Point-in-time schema, migrations, seed, idempotent writes |
| `features` | 33 features produced from persisted evidence only, `observed_at <= decision_time` |
| `risk_engine` | Hard rejects (`BLACK`) and traffic-light risk states |
| `scoring` | Ten separated scores + explanation records + alerts |
| `backtest` | Replay-safe historical scans with no leakage |
| `api` / `ui` | Typed read endpoints + Streamlit operator terminal |

### New modules to build by hand

```
radar/        On-chain + venue discovery at t0 (new pools, new mints, funding events)
mempool/      Free mempool/transaction log watchers (first buys, sniper detection)
narrative/    Narrative Radar: crawl cheap public channels, cluster, measure velocity
catalyst/     Catalyst Timetable: TGE/ICO/airdrop/unlock/listing schedule extraction
fingerprint/  Syndicate fingerprinting: who launches these tokens, recidivism score
pump_physics/ Hype-lifecycle state machine: phase detection + transition events *(built:
            `pump_physics/` deterministic phase detection from persisted evidence,
            monotonic idempotent `lifecycle_events`, `lifecycle_phase` feature feeding
            risk/exit scoring, `/lifecycle/events` + `/lifecycle/current` API, Lifecycle
            Radar UI)*
forecast/     Prediction layer: phase-transition probabilities + survival models
ops/          ntfy alerts *(built)*, Parquet archive compactor + retention
            pruning over MinIO/local disk + DuckDB lake queries *(built)*,
            retention autopilot: cadence-gated compaction + pruning with
            lake-growth Feed Health reporting *(built)*
```

---

## 4. `radar/` — Discovery before the pool exists

The biggest alpha edge is **pre-listing discovery**: a token you have pre-scored before
its pool exists is a token you're not racing to scan at t0.

**Pre-launch queue (new):**
- Watch launchpad/TGE schedules (Jupiter LFG, pump.fun announcements, launchpad pages)
  — these are public HTML/JSON; store every scheduled event in `Catalyst`.
- Watch contract deployments on-chain: new mint creation + metadata write + LP add is the
  t0 sequence. Poll RPC `getSignaturesForAddress` for known factory programs, or watch
  GeckoTerminal `new_pools` (already in MVP) and backfill the asset record.
- Watch **dev identity**: new projects from known devs/wallets (see `fingerprint/`) get
  pre-queued even before any social signal.
- Every pre-launch candidate gets a **pre-score** with `confidence` suppressed until real
  market evidence exists — missing data raises `UncertaintyScore`, never fake zeros.

**Ignition detection (t0 within minutes):**
- GeckoTerminal `new_pools` + DexScreener profiles (already present) as the coarse net.
- **The fine net is on-chain:** for each newly discovered mint, poll
  `getSignaturesForAddress` for buy transactions. A burst of sniper buys in the first
  seconds → `mempool/` flags it and the token jumps the research queue with a
  `SNIPER_BURST` catalyst.
- Track the **first liquidity injection size and its source wallet**. An LP add of
  $50k+ from a wallet with a history of successful pump launches is a far stronger
  ignition signal than $5k from a fresh wallet.

---

## 5. `mempool/` — The quiet leading indicator

Paid tools sell "real-time alerts." You can build the equivalent free:

- **Solana:** poll `getSignaturesForAddress` on the mint (free RPC, every 2–5s, token
  bucket limited) — this is a poor man's geyser and is *plenty* for ignition detection.
  Optionally subscribe to Jito's public block-engine endpoints for pending-transaction
  visibility on new mints.
- **EVM (Base/Ethereum):** public WebSocket RPCs for `newPendingTransactions` on the
  Uniswap/Pancake router addresses, or poll public RPC `eth_getLogs` on the factory
  `PairCreated` event — the pool exists before the first UI shows it.
- **Sniper fingerprinting:** cluster the first-buy wallets across tokens. Bots buy
  within ~1 second of pool creation; wallets that do this repeatedly across tokens are
  marked as known snipers. A token whose first 20 buys come from 3 known sniper clusters
  is a coordinated launch, not organic demand.

The mempool module writes `RawEvidenceItem` rows with sub-minute `observed_at` — the
whole point-in-time discipline of the existing engine applies, so backtests can replay
"what did we know at t0+3min."

---

## 6. `narrative/` — The Narrative Radar (replaces LunarCrush entirely)

Hype is manufactured *before* it hits price. The radar measures the manufacturing:

**Free sources to crawl (all public, all local):**
- **Reddit:** `https://www.reddit.com/r/{sub}/new.json` — public JSON, no key, needs a
  proper User-Agent. Track crypto subs + niche narrative subs (AI, meme, gaming).
- **Telegram:** public channel message history via a Telethon user session (free account,
  public channels only, respect ToS). Crypto shill groups are where launches get seeded.
- **YouTube:** channel RSS (`https://www.youtube.com/feeds/videos.xml?channel_id=...`) —
  free, no key. KOL video velocity is a leading indicator for a narrative.
- **GitHub:** search API (free unauthenticated ~60 req/hr — enough) — watch new repos
  for token names, sudden star velocity.
- **HuggingFace:** free — AI-narrative tokens almost always have an HF page before the
  pool.
- **X/Twitter:** RSSHub (self-hosted, free) + Nitter mirrors — flaky by nature, so it is
  a *bonus* source, never a dependency. Feed health tracking makes flakiness visible,
  not fatal.

**Processing (no paid LLM needed):**
- Dedupe with title/URL hashes (schema already has `title_hash`, `url_hash`).
- **Narrative clustering:** embed mentions with a local `sentence-transformers` model
  (free, offline), cluster by week, and track cluster growth. A cluster growing 10x in a
  day with a token name inside is narrative acceleration.
- **Lexicon baseline:** a hand-built hype lexicon (pump words: "x100", "gem", "presale",
  "whitelist", "fair launch", "moonshot") with per-cluster frequencies. This gives you
  velocity metrics with zero model cost; the local LLM (optional Phase 3) only refines
  classification and *stance*.
- Write everything to `SocialMention` / `NewsItem` with `asset_id` resolved when
  possible, else `topic` — the feature factory already handles both.

**New feature suggestions** (extend `features/definitions.py`):
- `prelaunch_narrative_velocity` — mentions of the token *before* first pool.
- `narrative_cluster_growth_7d` — the cluster growth rate.
- `shill_channel_diversity` — how many *independent* channels mention it (independent
  organic vs. one-syndicate shill).
- `kol_velocity` — distinct KOL channels (YouTube channel ids + Telegram handles)
  mentioning the symbol in the last 24h. *(built: computed from persisted crawler
  metrics; feeds the hype score as shill breadth.)*
- `github_star_velocity` and `hf_download_velocity` — dev-activity proxies for the
  "AI-adjacent hype" class of tokens. *(built: per-repo/per-model cumulative-star and
  download deltas across raw-evidence crawls, scaled to per-day rates, max across
  the repos/models the symbol is mentioned with; feed the catalyst score.)*

**DuckDB lake read path** (`features/lake.py`): the market/liquidity block
(`five_min_return`, `one_hour_return`, `volume_acceleration`, `liquidity_depth`,
`liquidity_change`, `buy_sell_ratio`, `unique_buyers_estimate`, `pair_age_minutes`,
`spread_estimate`, `volatility`, `venue_agreement`) is also computable directly from
the archived Parquet lake via DuckDB instead of hammering the hot DB. The lake path
reconstructs the normalized market/liquidity series from the GeckoTerminal
`new_pools` evidence payloads (same h1|h24|m5 window precedence, same hourly floor,
same first-wins dedup as ingestion) and feeds the *shared*
`compute_market_block` math the SQL path uses — so the two read paths are provably
identical, enforced by the parity test in `tests/test_lake_features.py`. The lake
path also reconstructs the on-chain features from the archived RPC evidence:
`holder_count` / `holder_growth` / `top_holder_concentration` from the
`solana_rpc` holder-snapshot payloads (`largest_accounts` + `supply`, deduped per
hour/wallet like `insert_holder_once`) and `suspicious_contract_flags` as the
count of evidence-backed `low_liquidity` scans (`reserve_in_usd` below
`min_discovery_liquidity_usd`). Reconstructions are cached per `(asset, hour)`
at the class level (bounded LRU, shared across factory instances), so repeated
backtest steps over the same window never re-query DuckDB; sub-hour decision
times bypass the cache and `LakeFeatureFactory.clear_cache()` drops it. A
**daily parity CI job** (`ops/parity.py`, `PARITY_FREQUENCY_HOURS`) runs the
same comparison against the full production lake at a decision time provably
inside the archived window and pages any divergence via ntfy
(`Serpent Circle - Lake Parity Mismatch`), so a payload-shape change or
reconstruction drift that would silently skew lake-replayed backtests is
surfaced instead of quietly corrupting results.

---

## 7. `catalyst/` — The Timetable (predicting *when*)

Spikes are scheduled events more often than not. A token about to be listed, airdropped,
or unlocked has a public schedule:

- TGE/ICO dates from launchpad pages and presale sites (public HTML).
- Exchange listing announcements (exchange blogs/RSS, public announcements).
- Airdrop claim dates and **unlock schedules** (tokenomics docs, public APIs).
- Pump.fun / launchpad "graduation" milestones.

Each becomes a `Catalyst` row with `scheduled_at`, `confidence`, and a source ref. The
`catalyst` score channel already exists — wire the timetable into it, and emit
`UPCOMING_CATALYST` alerts **N days before** the event. The operator's edge is
"pre-positioned awareness," not pre-positioned capital.

---

## 8. `fingerprint/` — The Syndicate Recidivism Score (the killer feature)

The same pump-and-dump syndicates launch token after token. If you fingerprint their
behavior once, you can predict their next launch:

**What to fingerprint per cluster** (extend the existing `WalletCluster` schema):
- Deployer wallets (Solana: the fee payer of the mint tx; EVM: tx.origin of the
  `PairCreated` event) — already partially in `Contract.deployer_wallet`.
- Funder wallets (who sent the initial liquidity).
- Sniper bot clusters (from `mempool/`).
- LP-removal wallets (who pulls liquidity at the top).
- Shared code patterns (same bytecode hash, same mint metadata template, same website
  template, same shill channel roster).

**Recidivism score:** given a new token, compute the overlap of its launch wallet set
with known clusters, weighted by how often those clusters' past tokens pumped then
collapsed. A high recidivism score + `BLACK`-adjacent contract flags = near-certain
collapse; the engine should *pre-flag* these tokens at t0 with an `EXIT_RISK` alert
before the pump even finishes. This is prediction with a mechanism behind it, not
pattern-matching noise.

**Network analysis:** build a wallet→token bipartite graph (networkx is already a dep).
The connective tissue between "new token" and "known bad actor" is the graph path length;
score tokens by graph proximity to toxic clusters.

---

## 9. `pump_physics/` — The Lifecycle State Machine

Each tracked token moves through `SEEDING → IGNITION → PARABOLIC → SATURATION → COLLAPSE`
(exits: `DEAD`, `RUGGED`, `SURVIVOR`). The state machine consumes features and emits
**transition events** — these events are the prediction targets:

| Transition | Leading observables (free data) |
|---|---|
| SEEDING → IGNITION | pre-launch narrative velocity ↑, dev activity ↑, liquidity injection, sniper burst, syndicate fingerprint match |
| IGNITION → PARABOLIC | volume acceleration, buy/sell ratio ↑, holder growth accelerating, venue agreement ↑ |
| PARABOLIC → SATURATION | **holder growth decelerating while price rises** (new buyer exhaustion), volume/price divergence, buy/sell ratio rolling over |
| SATURATION → COLLAPSE | **LP withdrawal / burn detected**, dev/funder wallet movement, unlock window, sell pressure > buy pressure, shill channels going quiet (the "last shill" signal), holder count plateau |

Collapse is preceded by *exits*: money leaving the pool, insiders leaving the chat. The
state machine watches exits, not prices. `ExitRiskScore` already exists — the machine
makes it *predictive* instead of descriptive by feeding it transition probabilities.

---

## 10. `forecast/` — The Prediction Layer

The existing `scoring/` measures the present. `forecast/` predicts the future, with
calibration:

**Phase-transition classifiers (free, local):**
- Gradient boosting (LightGBM or CatBoost, both free and already sk-learn compatible) or
  scikit-learn's HistGradientBoosting — no GPU needed for tabular data this size.
- Target 1: `P(ignition within 24h)` for pre-launch candidates.
- Target 2: `P(collapse within 24h)` for ignited tokens.
- Targets are **labeled from backtest history** (collapse = ≤ -70% within 24h peak-to-
  trough, matching the existing `collapse_return_pct`), with strict point-in-time feature
  construction — the existing `observed_at <= decision_ts` rule is the contract.

**Survival analysis (the crafty part):**
- Use a local survival library (lifelines, free) on the labeled arcs: `time-until-peak`
  and `time-until-collapse` as time-to-event models. Instead of "price will be X," emit
  **"probability the peak happens within the next N hours"** — which is the actually
  actionable quantity and far easier to calibrate than a point price forecast.
- Censoring is handled honestly: tokens still alive at the end of the observation window
  are right-censored, not treated as successes or failures.

**Calibration:**
- Isotonic regression (scikit-learn) on out-of-fold probabilities so that "70% collapse
  probability" really means collapse 70% of the time. **Never show an uncalibrated
  number to the operator.** The `ConfidenceScore` and `UncertaintyScore` channels already
  encode the same philosophy for the rule-based scores.

**The HypeForecast artifact:** per token, per decision time:
- `p_ignition_24h`, `p_collapse_24h`, `expected_time_to_peak` (median + CI),
  `expected_time_to_collapse`, `calibration_bucket`, and a short machine-readable reason
  list — persisted as new tables so every forecast is replayable and auditable.

---

## 11. Backtest & Evaluation — proving it works before trusting it

The existing `backtest/` runner is replay-safe; extend it into the engine's conscience:

- **Label arcs, not points:** each token's full lifecycle (times of ignition, peak,
  collapse) stored as `Label` rows from price history — the labels that supervised
  learning consumes.
- **Walk-forward validation** with purged train/test splits (no feature leakage across
  time; scikit-learn's `TimeSeriesSplit` with a purge gap ≥ the forward horizon).
- **Metrics that matter:**
  - `precision@k` for ignition flags (existing: `precision_at_10`).
  - **Lead time:** minutes between flag and pump start — the actual alpha.
  - **Collapse-warning lead time:** minutes between exit alert and -70%.
  - False-alarm rate (calibration check).
- **Drift monitoring:** monthly re-run of backtests over the last 90 days to detect when
  the market's hype mechanics change and the model needs retraining.
- Every `BacktestRun` already stores `git_sha` + `model_version` — keep that; forecast
  models get their own version registry.

---

## 12. Local-First Stack & Zero-Cost Operations

**Keep:** Postgres (point-in-time source of truth), Redis (queues), MinIO (Parquet raw
evidence), FastAPI, Streamlit, APScheduler.

**Add for zero-cost operations:**
- **DuckDB** (already a dependency) as the analytics engine over MinIO Parquet — the
  feature-engineering pipeline reads Parquet lakes instead of hammering Postgres.
- **Parquet retention policy:** raw evidence older than N days is compacted and
  partitioned by `(source, year, month)`; a `ops/` job prunes hot storage.
- **SQLite fallback profile:** a `env=local-single` config that swaps Postgres for
  SQLite and Redis for in-process queues, so the whole engine runs with **zero
  containers** on one laptop. Docker remains the "full rig" profile.
- **Alerts:** write `Alert` rows (existing) → `ops/` notifier pushes to **ntfy.sh**
  (free, no account) or a self-hosted Gotify; email via Apprise only if the user wants it.
- **Scheduling:** APScheduler in-process (present) or systemd timer / Task Scheduler —
  no hosted cron.
- **Local LLM (optional):** Ollama + a ~8B Qwen/llama instruct model on the operator's
  machine for narrative classification. Everything degrades gracefully to the lexicon
  baseline when the model is offline.

**Cost envelope:**

| Item | Monthly cost |
|---|---|
| All ingestion, scoring, forecasting, backtest, UI | $0 (your machine) |
| Optional 24/7 VPS (2 vCPU / 4GB) running the full rig | ~$5 |
| Optional Blockscout / Erigon self-hosted node | $0 on existing hardware |
| Push notifications (ntfy.sh) | $0 |
| **Total** | **$0–$5** |

---

## 13. Guardrails — what this engine will never do

- **No trading or custody.** The output is a calibrated forecast and evidence trail; the
  operator decides. (The docs already forbid auto-trading; keep it that way.)
- **No paid dependencies as required paths.** Every paid key in `.env` is an
  enhancement; the default path must always run fully free. If a free endpoint dies, the
  engine degrades to fewer sources and *raises* `UncertaintyScore` — never fakes data.
- **No hindsight.** Backtests and forecasts only ever use rows with
  `observed_at <= decision_ts`. A late-arriving record is future knowledge, not evidence.
- **No black-box worship.** Every ML output must carry machine-readable reasons mapped
  back to features and evidence rows. If a model can't explain itself, the engine uses
  the rule-based scores instead.
- **No uncalibrated confidence.** Probabilities are shown only after isotonic
  calibration on out-of-fold data, and the calibration bucket is displayed with the
  number.
- **Respect ToS.** The craft is public endpoints, RSS bridges, and on-chain public data —
  never login-scraping or endpoint hammering.

---

## 14. Roadmap

- **Phase 0 (now):** MVP rules engine, source hierarchy, replay-safe backtests. *(done)*
- **Phase 1 (radar + mempool + fingerprint + pump_physics):** t0 ignition detection, sniper burst flag,
  pre-launch queue, syndicate recidivism score, LP-withdrawal watcher, and the hype-lifecycle
  state machine. This alone makes
  the engine *leading* instead of *lagging*. *(built in full: `radar/` ignition scanner +
  withdrawal watcher + `prelaunch/` queue, `mempool/` Solana signature watcher + EVM
  factory log watcher, and `fingerprint/` cluster learning + recidivism assessment —
  all wired into the ingestion pipeline, exposed via API, and feeding
  `ignition_signal`, `liquidity_withdrawal_signal`, `prelaunch_priority`,
  `recidivism_score`, and `lifecycle_phase` features into risk scoring. The
  `pump_physics/` state machine advances SEEDING → IGNITION → PARABOLIC →
  SATURATION → COLLAPSE (exits DEAD/RUGGED/SURVIVOR) from persisted evidence,
  persists idempotent transition events, and surfaces them via `/lifecycle/*`
  and the Lifecycle Radar UI.)*
- **Phase 2 (narrative radar + catalyst timetable):** Reddit/Telegram/YouTube/GitHub/HF
  crawl, narrative clustering with local embeddings, TGE/airdrop/unlock schedules, exit
  "last shill" signal. *(built: `narrative/` free crawlers — Reddit public JSON, YouTube
  RSS, GitHub search, HuggingFace trending, RSS — with a hand-built offline minhash
  clusterer (no model download; sentence-transformers is a documented upgrade path),
  and `catalyst/` rule-based extraction + upcoming-catalyst alerts feeding
  `narrative_cluster_growth_7d`, `shill_channel_diversity`,
  `prelaunch_narrative_velocity`, `kol_velocity`, `github_star_velocity`, and
  `hf_download_velocity` features. Telegram
  public-channel crawling via Telethon is built as a gated extension: free API
  credentials from my.telegram.org, a one-time interactive session login
  (`scripts/telegram_auth.py`), public broadcast channels only (`username` set +
  `broadcast` true), per-channel rate-limit pauses plus Telethon flood-sleep, and
  honest health-yellow degradation when credentials or an authorized session are
  missing.)*
- **Phase 3 (forecast):** phase-transition classifiers, survival models, isotonic
  calibration, walk-forward evaluation, drift monitoring. Rule scores stay as the
  conservative floor. *(built: `forecast/` point-in-time label generation, gradient
  boosting with purged train/test splits, isotonic calibration, hand-built discrete
  hazard models for expected time-to-peak and time-to-collapse, `forecast.*`  backtest metrics, and per-asset `Forecast` rows feeding `collapse_probability_24h` into
  RiskScore/ExitRisk. The training feature set (`FORECAST_FEATURE_NAMES`, the full
  point-in-time set) includes the narrative dev-activity proxies `kol_velocity`,
  `github_star_velocity`, and `hf_download_velocity`, so dev-activity evidence shapes
  the phase-transition probabilities, and `lifecycle_phase` conditions the survival
  layer: training buckets samples by the asset's phase at the label's decision time
  and fits per-phase time-to-collapse and time-to-peak hazard curves, so a token in
  COLLAPSE decays on a fast curve while a SEEDING token rides the long tail
  (`details.hazard_phase` records which curve each forecast used; unlearnable phases
  fall back to the global aggregate). Backtest runs now record `git_sha` and the lead-time metrics —
  median ignition lead minutes, median collapse-warning lead minutes, and
  false-alarm rate. Drift monitoring compares
  trailing-window precision and calibration error against the run baseline and
  persisted history and emits `forecast_drift` health warnings (yellow on drift, red
  when trailing precision collapses). Degrades to health-`yellow` with no predictions
  until enough labeled history accumulates.)*
- **Phase 4 (self-hosting):** SQLite single-machine profile, optional self-run RPC +
  Blockscout, Parquet compaction, full $0 stack validation. *(built: `env=local-single`
  zero-container profile — `scripts/bootstrap_local.py` creates the SQLite DB, seeds
  reference data, and prepares the local Parquet lake; `ops/archive.py` compacts raw
  evidence into `source/year/month` partitioned Parquet over MinIO (docker) or local
  disk (zero-container), marks rows `archived_at`, prunes unreferenced rows older than
  `ARCHIVE_RETENTION_DAYS`, exposes the lake to DuckDB via `python -m ops.archive
  --query`, and reports `component:archive` health. Compaction runs on a
  **per-partition schedule**: each pass computes the `(source, year, month)`
  partitions whose evidence has aged past `ARCHIVE_COMPACT_AFTER_HOURS` and
  compacts exactly those (zero work when nothing is due). The ingestion worker
  never touches the archive — the retention autopilot (`ops/retention.py`)
  fully owns compaction + pruning on `RETENTION_CADENCE_HOURS` (APScheduler
  job, worker-loop cadence check, or a systemd timer /
  Task Scheduler entry invoking `python -m ops.retention --once`, or run one
  pass by hand via `make retention`), and manual compaction-only passes run
  standalone via `make archive`. Each pass persists one
  `RetentionRun` with lake totals + `growth_bytes`/`growth_pct`, and reports
  the growth in Feed Health as `component:lake`. `GET /archive/manifests`
  + the **Archive & Retention** UI view surface the lake. A full $0 stack
  validation runbook lives in `docs/validation.md` §Phase 4.)*

**North star metric:** *median minutes of advance warning for collapse onset at a 70%
recall and <30% false-alarm rate, measured on a walk-forward backtest.* If the engine
can't prove that on its own history, it doesn't get to whisper in the operator's ear.
