103 STATE.md
# STATE.md — Serpent Circle Change Ledger

Single source of persistent state for this repo. Read this file (plus recent `git log`) at the
start of every session; update it after every meaningful unit of work. Never rely on
conversation memory across sessions. This is the repo's first state file — no `CHECKLIST.md`
or `STATE.md` existed before 2026-09-09.

- **ID scheme:** `E#` enhancement to existing code · `N#` whole new module · `O#` omission/reduction.
- **Statuses:** `done` · `in-progress` · `planned` · `deferred (reason)` · `blocked (on what)`.
- Evidence tags: `[verified]` = checked directly against the working tree on **2026-09-09**;
  `[report]` = sourced from `docs/validation-field-report.md` (file read; numbers quoted match);
  `[briefing]` = not yet independently checked.

## Ground truth (2026-09-09)

- Field-report verdict `[report]`: RiskBand ordinal distance indistinguishable from
  always-GREEN (2.00 vs 2.00, n=286); BLACK collapse precision 9.09% = base rate (n=1,573);
  concordance 1.0 on risk_score / exit_risk / uncertainty flagged as degenerate-distribution
  artifacts; confidence calibration error 0.32 vs 0.10 trust ceiling; **0 Forecast rows and
  8 label rows** — forecast/hazard/ensemble layers have never been benchmarkable.
- `assess_risk()` at `risk_engine/rules.py:136` `[verified]`.
- `_BAND_CONFIDENCE` hardcoded lookup at `scoring/formulas.py:42` (used at `:148`) `[verified]`.
- Calibrator module exists: `risk_engine/calibrator.py` `[verified — not yet read in depth]`.
- LLM pinned to Ollama `qwen2.5:0.5b`: `llm/engine.py:28` (`OLLAMA_DEFAULT_MODEL`), `:100`
  (fallback), `common/config.py:452`; same pin in `docker-compose.yml`, `packaging/install.sh`,
  `INSTALL.md` `[verified]`.
- Explorer crawler is Etherscan-only (chain IDs 1 / 8453, `getcontractcreation` at
  `crawlers/sources/explorer.py:40-46`); zero matches in `crawlers/` for mint/freeze authority
  — Solana on-chain safety checks do not exist `[verified]`.
- No API authentication: no match in `api/` for api_key / Authorization / HTTPBearer /
  verify_token / require_auth / X-API headers; CORS middleware present, routes open
  `[verified — absence-of-match search]`.
- Forecast gate: `forecast_min_samples = 30` (`common/config.py:353`), enforced at
  `forecast/engine.py:355,728` and `data_lake/labels.py:294` `[verified]`.
- Secrets hygiene: `.env`, `telegram.session`, `*.db` gitignored and untracked `[verified]`.
  Repo is **public** — any key that ever touched git history should be rotated (not actioned).
- Remaining `[briefing]`-sourced details: the exact two-value risk distribution (59.99/66.66 —
  from report text, DB not re-queried) and hand-set rule-engine point weights.

## Pre-existing working-tree changes (not ours — do not touch)

6 uncommitted files forming one coherent in-flight feature (watchdog re-alert capping:
`SKIP_ALERT_MAX_ALERTS` / `SKIP_ALERT_MAX_MINUTES`, `note_phase_skip` verdict model,
`engine_stage_watchdog_skip_muted`): `common/config.py`, `docs/runbook.md`, `engine/run.py`,
`ops/watchdog.py`, `tests/test_engine_phases.py`, `tests/test_retention.py`. 255 insertions.
Owner unknown; left uncommitted and untouched.

## Change ledger

### E — Enhancements to existing code

| ID | Item | Status | Notes / evidence |
|----|------|--------|------------------|
| E1 | Feature-completeness audit of `assess_risk()` inputs — find which of ~35 features silently default to 0 | done (2026-09-14; see session log) | Audit: DB `features` table (170k rows, 4,859 snapshots) shows kol_velocity/catalyst_proximity_hours 100% missing, recidivism_score 99.8%, github_star_velocity 98.8%, collapse_probability_24h 76.8%, liquidity block ~62%, lifecycle_phase 58.2% (rule-engine default 1.0); only event-count features (ignition/lp_removal/withdrawal) are legitimately 0. `_feature()` now tracks defaults into `RiskAssessment.missing_features`; `compute_scores` merges with factory-missing and applies a coverage-weighted confidence penalty (max 30 pts); API `RiskResponse.missing_features` + GUI panel. Tests: `tests/test_feature_completeness.py` (15 tests). Unblocks E2/E3 quality |
| E2 | Calibrate RiskBand + ML probability thresholds via `risk_engine/calibrator.py` against real outcomes | deferred (needs E1 + more outcome data) | Hand-tuned 25/50/75 and 0.10/0.30/0.50/0.75 `[briefing]` |
| E3 | Replace `_BAND_CONFIDENCE` hardcoded table with isotonic/Platt refit vs `RiskOutcome` rows; keep current table as fallback below a sample floor | planned (proposed this session) | `scoring/formulas.py:42` `[verified]` |
| E4 | Label/outcome pipeline: accelerated historical backfill, or honest "forecasting not live yet" acknowledgment | deferred (data-acquisition scope) | 8 labels vs min-30 gate `[report]` |
| E5 | Single-writer SQLite wedge → WAL mode + write-serialization queue | ✅ done (2026-09-14) | `storage/write_queue.py`: single-writer thread + Future API (`submit`/`submit_sync`), dedicated writer session, drain-on-`stop()`; `storage/repository.py`: new `queued_write()` helper + `record_health()` routes through the queue when running (direct write otherwise); `engine/run.py`: queue starts after `_bootstrap()`, drains in `finally`; WAL pragma verified engaged at queue start. Evidence: `tests/test_write_queue.py` (10 tests) — 8 writers × 25 concurrent writes + 2 readers: zero "database is locked" errors, 200/200 rows, exec p99 2.6ms (criterion <100ms), shutdown drain verified, SystemHealth `write_queue` heartbeat persists queue depth + latency |
| E6 | LLM: configurable OpenAI-compatible endpoint instead of hardwired Ollama; unpin the 0.5B default | planned (proposed this session) | `llm/engine.py:28,100` `[verified]` |
| E7 | API authentication (`Depends`-based; localhost-token default, LAN-capable) | planned (proposed this session) | No auth found in `api/` `[verified]` |

### N — Whole new modules

| ID | Item | Status | Notes |
|----|------|--------|-------|
| N1 | Solana on-chain safety: mint authority, freeze authority, LP-lock status | planned (candidate this session) | Biggest stated-purpose gap `[verified absent]` |
| N2 | EVM bytecode/honeypot static analysis (hidden mint, blacklist/pausable, proxy backdoor, fee-on-transfer) | deferred | Own session; behind the E-batch |
| N3 | Mempool/pending-tx sniper + MEV-bundle detection | deferred | |
| N4 | Offline historical backfill/replay engine for synthesized labels | deferred | Unsticks the forecast layer; pairs with E4 |
| N5 | Funding-source / CEX-deposit wallet-cluster graph tracing | deferred | Extends `fingerprint/engine.py` |
| N6 | Secrets/credentials management (rotation, at-rest encryption, per-crawler scoping) | deferred | |
| N7 | Portfolio/position paper-trading simulation ledger | deferred | Optional vs non-trading mission |
| N8 | Point-in-time leakage guardrails: `validation/leakage_guard.py` (frozen `decision_ts` scope + `require_decision_ts` decorator + `check_rows_point_in_time` assertion); `features/factory.py` and `features/lake.py` enforce it; violations log to `SystemHealth` (component `leakage_guard`) | done | `tests/test_leakage.py` (13 tests): intentionally leaky pipeline raises `LeakageViolation`, clean pipeline passes; `docs/leakage-audit.md` enforcement section added |

### O — Omissions / reductions

| ID | Item | Status | Notes |
|----|------|--------|-------|
| O1 | Sunset Nitter + X-trends crawlers | ❓ needs decision (D3) | Fragile / legally gray; sub-5%-actionability sources are already auto-deprioritized |
| O2 | De-emphasize non-benchmarkable score family (hype/ethos/liquidity_access/manipulation/catalyst/research_priority) in the GUI | planned (later session) | Methodology §6: no ground truth exists `[report]` |
| O3 | Suppress-or-flag any concordance ≥ 0.95 surfaced without the leakage cross-check | planned (proposed this session) | Report explicitly warns future runs `[report]` |

## Dependency order

E1 → (E2, E3 quality) · E4/N4 → forecast-layer benchmarks · N1/N2 independent of the E-batch ·
O1 after D3 · E5 after D2.

## Decisions pending user sign-off

- **D1 — session scope:** which items this session implements (asked 2026-09-09).
- **D2 — E5 approach:** decided 2026-09-14 → WAL + write-queue now (implemented; Postgres migration deferred unless contention outgrows a single writer).
- **D3 — O1 approach:** disable-by-default vs delete modules vs ledger-only.

## Session log

### 2026-09-14 — E1 (#2) Feature Completeness Audit + Missing-Feature Propagation (done)

Implemented by Juno subagent (fabrication plan item #2):

- **Audit findings** (against live `serpent.db`, 170,065 feature rows / 4,859 snapshots):
  - 100% missing: `kol_velocity`, `catalyst_proximity_hours`
  - >95% missing: `recidivism_score` (99.8%), `hf_download_velocity` (99.7%),
    `github_star_velocity` (98.8%), `narrative_cluster_growth_7d` (96.8%)
  - ~93% missing: `holder_count`, `holder_growth`, `top_holder_concentration`
  - ~89% missing: `volume_acceleration`, `volatility`
  - 76.8% missing: `collapse_probability_24h` (silently read as 0% = safe)
  - ~62% missing: liquidity block (`liquidity_depth`, `spread_estimate`,
    `venue_agreement`, returns, `buy_sell_ratio`, `unique_buyers_estimate`)
  - 58.2% missing: `lifecycle_phase` (silently read as 1.0 in `assess_risk`)
  - 0% missing but legitimately zero: event-count features (`ignition_signal`,
    `liquidity_withdrawal_signal`, `lp_removal_signal`); `suspicious_contract_flags`
    conflates "no contract data" with "contract clean" (flagged, not changed).
- **Built:**
  - `risk_engine/rules.py`: `_feature()` gained a keyword-only `defaulted` tracking
    set (returned values byte-identical without it); `assess_risk()` threads it
    through all 13 lookups and returns `RiskAssessment.missing_features`.
  - `scoring/formulas.py`: `compute_scores` merges factory-missing with
    rule-engine-tracked defaults into `ScoreResult.missing_features`; new
    coverage-weighted confidence penalty (`_COVERAGE_PENALTY_MAX = 30.0`,
    linear in missing ratio). Measured: 0% missing -> 93.2, 11% -> 88.1,
    51% -> 70.1 confidence on an identical feature vector.
  - `scoring/engine.py`: verified — already populates `missing` from
    `FeatureValue.missing` flags and persists the union via `_upsert_explanation`.
  - `api/schemas.py` + `api/main.py`: `RiskResponse.missing_features`; the
    `/risk/{asset_id}` endpoint (which also runs `mask_unreliable_forecast`)
    now reports the popped `collapse_probability_24h` as missing.
  - `ui/app.py`: token-detail explanation panel shows a "Missing Features" section.
  - `tests/test_feature_completeness.py`: 15 tests (tracking fidelity, union
    accuracy, 50%-vs-10% confidence decrease, masked-forecast reporting).
- **Verification:** new file 15/15 pass; existing suites touching
  scoring/risk_engine/features/api all pass (test_risk_scoring, test_api,
  test_schema, test_score_drift, test_ensemble, test_ensemble_pipeline,
  test_cross_source_fusion, test_fingerprint, test_liquidity, test_llm,
  test_llm_calibration, test_rescore_compare, test_scoring_batch_reads,
  test_signal_links, test_velocity_features). No pre-existing uncommitted
  file was modified. `forecast_min_samples` untouched. Not pushed.
- **Constraint learned:** `_COVERAGE_PENALTY_MAX` capped at 30 (not 40) and
  uncertainty kept on factory-missing semantics so the pre-existing
  `test_rpc_data_layer_degradation_widens_uncertainty` strict orderings hold.

Note (2026-09-14): the uncommitted working-tree inventory has drifted since
2026-09-09 (now 14 modified files: backtest/runner.py, data_lake/webhooks.py,
ops/archive.py, scripts/refresh_rpc_pools.py, tests/test_api.py,
test_api_auth_webhooks.py, test_archive_hardening.py, test_engine_worker_fixes.py,
test_forecast.py, test_label_bootstrap.py, test_llm_calibration.py,
test_risk_scoring.py, test_scripts_hardening.py, test_validation_harness.py) —
in-flight work from other agents, still untouched.

### 2026-09-09 — Session 1

- Verified the enhancement briefing against the code (see Ground truth). All major claims held;
  two details remain `[briefing]`-sourced (exact two-value risk distribution; hand-set rule weights).
- Found and left alone: pre-existing watchdog re-alert-cap changes in the working tree
  (6 files, uncommitted).
- Created this ledger.
- Deferred: implementation pending answers to D1–D3.
