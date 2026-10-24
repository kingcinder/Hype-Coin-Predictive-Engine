103 STATE.md
# STATE.md — Serpent Circle Change Ledger

Single source of persistent state for this repo. Read this file (plus recent `git log`) at the
start of every session; update it after every meaningful unit of work. Never rely on
conversation memory across sessions. This is the repo's first state file — no `CHECKLIST.md`
or `STATE.md` existed before 2026-09-09.

- **ID scheme:** `E#` enhancement to existing code · `N#` whole new module · `O#` omission/reduction.
- **Statuses:** `done` · `in-progress` · `planned` · `deferred (reason)` · `blocked (on what)`.
- Evidence tags: `[verified YYYY-MM-DD]` = checked directly against the working tree on that date;
  `[report]` = sourced from `docs/validation-field-report.md` (file read; numbers quoted match);
  `[briefing]` = not yet independently checked.

## Ground truth (re-audited 2026-10-23)

- Field-report verdict `[report]`: RiskBand ordinal distance indistinguishable from
  always-GREEN (2.00 vs 2.00, n=286); BLACK collapse precision 9.09% = base rate (n=1,573);
  concordance 1.0 on risk_score / exit_risk / uncertainty flagged as degenerate-distribution
  artifacts; confidence calibration error 0.32 vs 0.10 trust ceiling; **0 Forecast rows and
  8 label rows** — forecast/hazard/ensemble layers have never been benchmarkable.
- `assess_risk()` at `risk_engine/rules.py:164` `[verified 2026-10-23]`.
- `_BAND_CONFIDENCE` hardcoded lookup at `scoring/formulas.py:42` `[verified 2026-10-23]`.
- `risk_engine/calibrator.py` exists and adapts risk thresholds; it does not replace `_BAND_CONFIDENCE` with a time-split confidence calibrator `[verified 2026-10-23]`.
- LLM pinned to Ollama `qwen2.5:0.5b`: `llm/engine.py:28` (`OLLAMA_DEFAULT_MODEL`), `:100`
  (fallback), `common/config.py:452`; same pin in `docker-compose.yml`, `packaging/install.sh`,
  `INSTALL.md` `[verified 2026-10-23]`.
- Explorer crawler is Etherscan-only (chain IDs 1 / 8453, `getcontractcreation` at
  `crawlers/sources/explorer.py:40-46`); zero matches in `crawlers/` for mint/freeze authority
  — Solana on-chain safety checks do not exist `[verified]`.
- API bearer-token authentication is implemented by `_check_bearer_token` and
  `_RequireApiTokenMiddleware` in `api/main.py:97-166`, with coverage in
  `tests/test_api_auth_webhooks.py` `[verified 2026-10-23]`.
- Forecast gate: `forecast_min_samples = 30` (`common/config.py:377`), enforced at
  `forecast/engine.py` and `data_lake/labels.py:320` `[verified 2026-10-23]`.
- `engine/run.py` uses `get_settings()` and the shared `storage.database.engine`; it does
  not force a backend. With no `.env` or DB environment override, settings resolve to
  PostgreSQL at `localhost:5432`. The checked-in root `.env` selects `local-single` and
  SQLite; Docker Compose now selects its PostgreSQL service `[verified 2026-10-23]`.
- Secrets hygiene: `.env`, `telegram.session`, `*.db` gitignored and untracked `[verified 2026-10-23]`.
  Repo is **public** — any key that ever touched git history should be rotated (not actioned).
- Full-history Gitleaks baseline: this clone is not shallow (`git rev-parse --is-shallow-repository`
  returned `false`), so no unshallow fetch was needed. The raw redacted scan examined 75
  commits and found two generic-key matches in webhook test fixtures; the exact-value test
  allowlist reduces the configured full-history scan to zero findings `[verified 2026-10-23]`.
- Remaining `[briefing]`-sourced details: the exact two-value risk distribution (59.99/66.66 —
  from report text, DB not re-queried) and hand-set rule-engine point weights.

## Working-tree snapshot (2026-10-23)

The canonical checkout was clean on `main` before this task began. The earlier
September 2026 note about six uncommitted watchdog files described a different
checkout state and is no longer current. Work for this task is on
`codex/hype-coin-predictive-engine`.

## Change ledger

### E — Enhancements to existing code

| ID | Item | Status | Notes / evidence |
|----|------|--------|------------------|
| E1 | Feature-completeness audit of `assess_risk()` inputs — find which of ~35 features silently default to 0 | done (2026-09-14; re-audited 2026-10-23) | `_feature()` tracks defaults into `RiskAssessment.missing_features`; confidence coverage penalty and API/UI missing-feature display remain in source. Evidence: `risk_engine/rules.py`, `scoring/formulas.py`, `api/schemas.py`, `ui/app.py`, `tests/test_feature_completeness.py` `[verified 2026-10-23]` |
| E2 | Calibrate RiskBand + ML probability thresholds via `risk_engine/calibrator.py` against real outcomes | deferred (no causally available training window) | Train-only isotonic threshold tuning is implemented; the live split has 0 causally available training rows, so thresholds remain at fallback values `[verified 2026-10-23]` |
| E3 | Replace `_BAND_CONFIDENCE` hardcoded table with isotonic/Platt refit vs `RiskOutcome` rows; keep current table as fallback below a sample floor | implemented; fallback active | Versioned isotonic persistence, runtime application, API/health status, and `RISK_CONFIDENCE_CALIBRATION_MIN_SAMPLES=100` are implemented; current 0-row train split prevents activation `[verified 2026-10-23]` |
| E4 | Label/outcome pipeline: historical backfill and honest forecast-status acknowledgment | planned (T6) | Forecast gate remains 30 in `common/config.py:377`; `scripts/backfill_history.py` has provider history fetchers but no label replay path `[verified 2026-10-23]` |
| E5 | Single-writer SQLite wedge → WAL mode + write-serialization queue | done (2026-09-14; re-audited 2026-10-23) | `storage/write_queue.py`, `storage/repository.py`, `engine/run.py`; behavior coverage in `tests/test_write_queue.py` `[verified 2026-10-23]` |
| E6 | LLM: configurable OpenAI-compatible endpoint instead of hardwired Ollama; unpin the 0.5B default | planned (T7) | `llm/engine.py` still calls the Ollama API; provider abstraction not present `[verified 2026-10-23]` |
| E7 | API authentication and network exposure (loopback defaults; explicit authenticated LAN binds) | done (2026-10-23) | Bearer middleware in `api/main.py:97-166`; `common/network.py` validates API/UI bind settings; GUI API/SSE clients send the configured token; tests cover CORS, startup rejection, defaults, and SSE `[verified 2026-10-23]` |
| E8 | History-divergence guard: pre-push hook blocks non-fast-forward pushes to `refs/heads/main` (`scripts/check_history_divergence.py` via `.pre-commit-config.yaml`, pre-commit env-var contract `PRE_COMMIT_FROM_REF`/`TO_REF`/`REMOTE_BRANCH` + raw-stdin fallback); CI `history-guard` job backstops push events (`--check github.event.before` vs `github.sha`, `fetch-depth: 0`); deliberate rewrites acknowledged via `ALLOW_HISTORY_REWRITE=1` (hook/CI) or `[history-rewrite]` head-commit marker (CI); branch-protection toggles documented in `ci.yml` header | done (2026-09-28; re-audited 2026-10-23) | `scripts/check_history_divergence.py`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, and `tests/test_history_divergence.py` remain present `[verified 2026-10-23]` |

### N — Whole new modules

| ID | Item | Status | Notes |
|----|------|--------|-------|
| N1 | Solana on-chain safety: mint authority, freeze authority, LP-lock status | done (2026-10-23) | `ingestion/solana_safety.py`, migration `0022_solana_safety_snapshots`, point-in-time feature factory, named risk weights, `/tokens/{id}`, and GUI evidence table; unsupported LP layouts remain unknown `[verified 2026-10-23]` |
| N2 | EVM bytecode/honeypot static analysis (hidden mint, blacklist/pausable, proxy backdoor, fee-on-transfer) | deferred | Dedicated static-analysis module is not present; `crawlers/sources/explorer.py` covers explorer lookups `[verified 2026-10-23]` |
| N3 | Mempool/pending-tx sniper + MEV-bundle detection | deferred | `mempool/` contains burst monitoring, but no sniper/MEV-bundle detector `[verified 2026-10-23]` |
| N4 | Offline historical backfill/replay engine for synthesized labels | planned (T6) | `scripts/backfill_history.py` fetches provider history; no point-in-time label replay is present `[verified 2026-10-23]` |
| N5 | Funding-source / CEX-deposit wallet-cluster graph tracing | deferred | `fingerprint/engine.py` has wallet-cooccurrence analysis, not funding-source/CEX tracing `[verified 2026-10-23]` |
| N6 | Secrets/credentials management (rotation, at-rest encryption, per-crawler scoping) | deferred | No dedicated secrets-management module; environment settings remain in `common/config.py` `[verified 2026-10-23]` |
| N7 | Portfolio/position paper-trading simulation ledger | deferred | No paper-position ledger module is present `[verified 2026-10-23]` |
| N8 | Point-in-time leakage guardrails: `validation/leakage_guard.py` and feature enforcement | done (re-audited 2026-10-23) | `require_decision_ts` and `check_rows_point_in_time` exist in `validation/leakage_guard.py`; tests and `docs/leakage-audit.md` remain `[verified 2026-10-23]` |

### O — Omissions / reductions

| ID | Item | Status | Notes |
|----|------|--------|-------|
| O1 | Disable Nitter + X-trends crawlers by policy | planned (T9; user-authorized) | Both `nightcrawler_nitter_enabled` and `nightcrawler_x_trends_enabled` default to `True` in `common/config.py` `[verified 2026-10-23]` |
| O2 | De-emphasize non-benchmarkable score family in the GUI and API | planned (T8) | GUI still sorts by hype/research priority; score-family validation metadata and badges are not present `[verified 2026-10-23]` |
| O3 | Suppress-or-flag concordance ≥ 0.95 surfaced without the leakage cross-check | done (2026-10-23) | `validation/presentation.py` suppresses values without a non-empty audit and marks audit findings; `/validation/latest` and the Validation Integrity GUI show the status `[verified 2026-10-23]` |

## Dependency order

E1 → (E2, E3 quality) · E4/N4 → forecast-layer benchmarks · N1/N2 independent of the E-batch ·
O1 after D3 · E5 after D2.

## Decisions pending user sign-off

- **D1 — session scope:** resolved 2026-10-23; this task authorizes T0–T10.
- **D2 — E5 approach:** decided 2026-09-14 → WAL + write-queue now (implemented; Postgres migration deferred unless contention outgrows a single writer).
- **D3 — O1 approach:** resolved 2026-10-23; keep code and disable both crawlers by default under T9.

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

### 2026-10-23 — T0: State and documentation reconciliation

- Audited every E/N/O ledger row against the current source tree and updated stale
  statuses/evidence dates. E7 was already implemented, so its stale `planned` status
  is now `done`; O1 is now planned as disable-by-policy under the authorized T9.
- Removed the obsolete uncommitted-watchdog snapshot; the checkout was clean before
  this task. Resolved the prior session-scope and crawler-policy decisions.
- Traced database selection: `engine/run.py` calls `get_settings()` and migrations
  use the shared engine from `storage/database.py`. No-env settings use local
  PostgreSQL; the repository's `.env` selects local-single SQLite. Updated README,
  INSTALL, `.env.example`, and Compose to describe PostgreSQL as the general default,
  SQLite as the explicit local-single profile, and Compose's PostgreSQL service.
- Fixed the clone directory in README and INSTALL. `docker compose config --quiet`
  and `git diff --check` passed. Post-task checks: pytest 752 tests, 0 failures,
  8 skipped; Ruff passed; mypy passed (52 files); broken-import scan passed
  (229 files); declared-deps scan passed (28 roots, 229 files).

### 2026-10-23 — T1: Network exposure hardening

- Changed API/UI default hosts to loopback. Added startup validation: any
  non-loopback bind requires `ENGINE_ALLOW_LAN=1`, a nonempty token, and bypass
  disabled; the validator emits a warning before accepting the configuration.
- Updated Makefile/systemd launchers and Compose. Compose uses explicit
  container binds, requires a token, and publishes ports on host loopback.
- Confirmed Starlette places CORS outside the auth gate: allowed-origin 401s
  have CORS headers and disallowed origins do not. Replaced the incorrect order
  comment. Added bearer headers to the GUI's API and SSE clients.
- TDD evidence: new startup/SSE tests failed before implementation for missing
  startup refusal and missing `Authorization`, then passed after implementation.
- Post-task checks: pytest 763 tests, 0 failures, 10 skipped; Ruff passed; mypy
  passed (53 files); broken-import scan passed (233 files); declared-deps scan
  passed (28 roots, 233 files); Compose config passed with a test token.

### 2026-10-23 — T2: Full-history secret scanning

- Added a staged Gitleaks pre-commit hook and a full-depth CI scan using the
  documented `.gitleaks.toml` allowlists. The allowlists are limited to exact
  demo values at `.env.example` and one fixed webhook test fixture; neither path
  is excluded wholesale.
- Added `docs/secrets.md` with the full rotation checklist for credential fields
  in `.env.example`, scan instructions, and the redacted baseline findings.
- Baseline: raw full-history scan (75 commits, ~3.06 MB) found two generic-key
  matches in historical test fixtures; configured scan found none. A current
  directory scan also found two detections in generated `.venv` JavaScript maps,
  which are untracked third-party artifacts and outside Git history.
- Verification: Gitleaks staged scan and pre-commit hook passed; pytest 763
  tests, 0 failures, 10 skipped; Ruff passed; mypy passed (53 files);
  broken-import scan passed (233 files); declared-deps scan passed (28 roots,
  233 files); pre-commit config validated; workflow YAML parsed with the
  secrets job's checkout depth confirmed as 0.
- Canary check on a disposable local branch: the staged hook rejected one
  synthetic high-entropy API key with redacted output; after committing that
  fixture only on the disposable branch, the full-history scan found one leak
  and exited nonzero. The branch and commit were discarded; the task branch is
  clean. GitHub-hosted CI itself was not run because no push was requested.

### 2026-10-23 — T3: Solana on-chain safety evidence

- Added pooled `getAccountInfo` collection for the bounded Solana holder-scan
  cohort and persisted timestamped mint/LP evidence in migration
  `0022_solana_safety_snapshots`. Mint decoding records authority state, raw
  supply, decimals, and Token-2022 transfer-fee, permanent-delegate,
  non-transferable, and default-account-state extensions.
- Unsupported LP account layouts remain `unknown`; parsed complete ownership
  data can classify LP supply as unlocked, locked, or burned. The token detail
  API and GUI show the latest raw snapshot, while scoring uses only snapshots
  with `observed_at <= decision_ts`.
- Added named heuristic rule weights (20 mint authority, 25 freeze authority,
  12 transfer fee, 20 permanent delegate, 10 non-transferable, 12 default
  frozen, 18 known-unlocked LP). RPC failures and unknown fields remain missing
  and contribute no safety points. No tests make live RPC calls.
- TDD evidence: decoder tests first failed because the safety module did not
  exist; feature/leakage tests first failed because the factory boundary was
  absent; token-detail test first failed because the API omitted the snapshot.
  Each targeted suite passed after implementation.
- Pre-task checks: pytest 763 tests, 0 failures, 10 skipped; Ruff passed; mypy
  passed (53 files); broken-import scan passed (233 files); declared-deps scan
  passed (28 roots, 233 files).
- Post-task checks: pytest 775 tests, 0 failures, 10 skipped; Ruff passed; mypy
  passed (53 files); broken-import scan passed (236 files); declared-deps scan
  passed (28 roots, 236 files). Targeted safety, feature, leakage, ingestion,
  API, and migration checks passed.

### 2026-10-23 — T4: Score distribution and validation integrity (done)

- Live read-only score diagnostic: 4,859 rows across 10 decision days; distinct
  values across all rows were 479 for risk, 682 for exit risk, and 21 for
  uncertainty. The latest stored days had 387/443/19 distinct values. Earlier
  days retain their old 2–7 value scores.
- Root cause verified in history: commit `1661a34` replaced fixed rule-point
  jumps and the 90-point hard-reject floor with proportional risk ramps and a
  40-point floor; four-decimal output resolution and tracked missing defaults
  are already present. T4 adds regression coverage rather than duplicating the
  existing formula correction.
- Added tests requiring >30 distinct risk scores from 100 varied fixtures and
  pure-noise concordance within the harness ±0.10 tolerance. Added a
  publication guard, read-only `/validation/latest` API, and Validation
  Integrity GUI. Missing leakage audit suppresses near-perfect concordance;
  suspicious cross-check results display with a warning.
- Generated `reports/validation-2026-10-23-t4.json`. Self-tests pass; live DB
  validation still reports risk/exit concordance 0.9687 (n=396) and 3
  point-in-time feature findings among 2,013 rows. API smoke result: both
  concordance values returned with `flagged_suspicious` status.
- Updated `docs/validation-field-report.md` with the historical score counts,
  current diagnostic, validation results, and unresolved leakage findings.
- Pre-task checks: pytest 775 tests, 0 failures, 10 skipped; Ruff passed; mypy
  passed (53 files); broken-import scan passed (236 files); declared-deps scan
  passed (28 roots, 236 files).
- Post-task checks: pytest 784 tests, 0 failures, 11 skipped; Ruff passed; mypy
  passed (53 files); broken-import scan passed (240 files); declared-deps scan
  passed (28 roots, 240 files).

### 2026-10-23 — T5: Time-ordered confidence calibration (done; fallback retained)

- Added a persisted isotonic confidence map trained only from completed,
  unambiguous `RiskOutcome` rows joined to their original `Score.confidence`.
  Training labels must be evaluated by the score-time split; the held-out
  window starts at or after that split. The configurable training floor is 100
  and activation requires held-out ECE ≤ 0.10 with at least 30 validation rows.
- Retuned risk score thresholds derive from training rows only and are persisted
  with a calibration that passes the held-out gate. Thresholds were not changed
  for the current DB because the causal training set is empty.
- Live `serpent.db`: 2,014 evaluated outcomes; 397 unambiguous. The score-time
  cutoff is 2026-08-24 06:00 UTC, with 0 training rows whose label was known by
  the cutoff and 82 later-scored validation rows. ECE is unavailable; no held-out
  performance claim is made and `_BAND_CONFIDENCE` remains active.
- Persisted attempt version `isotonic-confidence-2026-10-23` and report
  `reports/calibration-2026-10-23-t5.json`. API and `/health` smoke checks
  returned `fallback_insufficient_time_split` with fallback active.
- Pre-task checks: pytest 784 tests, 0 failures, 11 skipped; Ruff passed; mypy
  passed (53 files); broken-import scan passed (240 files); declared-deps scan
  passed (28 roots, 240 files).
- Post-task checks: pytest 795 tests, 0 failures, 11 skipped; Ruff passed; mypy
  passed (53 files); broken-import scan passed (244 files); declared-deps scan
  passed (28 roots, 244 files).
