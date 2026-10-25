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

## Ground truth (re-audited 2026-10-25)

- Read-only validation on a SQLite snapshot of the available local DB: RiskBand
  ordinal distance 2.202 (n=396) vs always-GREEN 1.798; BLACK collapse precision
  0.0884 equals the 0.0884 base rate (n=2,013); `risk_score` and `exit_risk`
  concordance 0.9687 (n=396), both flagged suspicious-good; confidence
  calibration error 0.0798 (n=2,013) vs 0.10 ceiling. The latest report still
  flags `pair_age_minutes`, `volume_acceleration`, and `liquidity_depth` on
  2,013/2,013 rows because `Feature.observed_at` is a materialization timestamp,
  not input-source availability. The finding remains surfaced pending a
  source-level audit `[report]`.
- Snapshot counts: 4,859 scores, 4,224 risk outcomes, 170,065 features,
  375 forecasts, and 522 labels `[verified 2026-10-25]`. Having persisted
  forecasts/labels does not make the forecast, hazard, or ensemble metrics
  benchmarkable; the current report's forecast/hazard/ensemble cells remain
  `insufficient_data` (n=0–2) `[report]`.
- No output is yet shown to beat noise: RiskBand is worse than its trivial
  always-GREEN baseline, BLACK precision is at the base rate, and the near-perfect
  score concordances remain suspicious-good `[report]`.
- `assess_risk()` at `risk_engine/rules.py:164` `[verified 2026-10-23]`.
- `_BAND_CONFIDENCE` hardcoded lookup at `scoring/formulas.py:42` `[verified 2026-10-23]`.
- `risk_engine/calibrator.py` exists and adapts risk thresholds; it does not replace `_BAND_CONFIDENCE` with a time-split confidence calibrator `[verified 2026-10-23]`.
- LLM provider is configurable between Ollama and OpenAI-compatible APIs; the
  `LLM_OLLAMA_URL` compatibility alias remains, and the model default is no
  longer pinned to a 0.5B model `[verified 2026-10-23]`.
- Solana mint/freeze and supported Token-2022 safety snapshots are collected by
  `ingestion/solana_safety.py`, persisted by migration 0022, and exposed through
  point-in-time features; unsupported LP layouts remain unknown
  `[verified 2026-10-23]`.
- API bearer-token authentication is implemented by `_check_bearer_token` and
  `_RequireApiTokenMiddleware` in `api/main.py:97-166`, with coverage in
  `tests/test_api_auth_webhooks.py` `[verified 2026-10-23]`.
- Forecast gate: `forecast_min_samples = 30` (`common/config.py:377`), enforced at
  `forecast/engine.py` and `data_lake/labels.py:320` `[verified 2026-10-23]`.
- `engine/__main__.py` sets `ENV=local-single` before importing settings, so a fresh
  `python -m engine` run without `.env` or DB overrides uses SQLite at `serpent.db`.
  `.env.example` is tracked and selects PostgreSQL; root `.env` is ignored local state
  and is not a checked-in file. Docker Compose selects its PostgreSQL service
  `[verified 2026-10-25]`.
- Secrets hygiene: `.env`, `telegram.session`, `*.db` gitignored and untracked `[verified 2026-10-23]`.
  Repo is **public** — any key that ever touched git history should be rotated (not actioned).
- Full-history Gitleaks baseline: this clone is not shallow (`git rev-parse --is-shallow-repository`
  returned `false`), so no unshallow fetch was needed. The raw redacted scan examined 75
  commits and found two generic-key matches in webhook test fixtures; the exact-value test
  allowlist reduces the configured full-history scan to zero findings `[verified 2026-10-23]`.
- Remaining `[briefing]`-sourced details: the exact two-value risk distribution (59.99/66.66 —
  from report text, DB not re-queried) and hand-set rule-engine point weights.

## Working-tree snapshot (2026-10-25)

The canonical checkout is on `main`; `origin/main` and `origin/fix/functional-pass`
are synchronized at the final state-ledger commit. The feature branch merged live
`origin/main` at `1d24499` (coverage chart update) in `a18ae3b`; then `main` was
fast-forwarded to the verified feature tip without rewriting history. The ignored
root `serpent.db` is existing user data and was not altered.

## Change ledger

### E — Enhancements to existing code

| ID | Item | Status | Notes / evidence |
|----|------|--------|------------------|
| E1 | Feature-completeness audit of `assess_risk()` inputs — find which of ~35 features silently default to 0 | done (2026-09-14; re-audited 2026-10-23) | `_feature()` tracks defaults into `RiskAssessment.missing_features`; confidence coverage penalty and API/UI missing-feature display remain in source. Evidence: `risk_engine/rules.py`, `scoring/formulas.py`, `api/schemas.py`, `ui/app.py`, `tests/test_feature_completeness.py` `[verified 2026-10-23]` |
| E2 | Calibrate RiskBand + ML probability thresholds via `risk_engine/calibrator.py` against real outcomes | deferred (no causally available training window) | Train-only isotonic threshold tuning is implemented; the live split has 0 causally available training rows, so thresholds remain at fallback values `[verified 2026-10-23]` |
| E3 | Replace `_BAND_CONFIDENCE` hardcoded table with isotonic/Platt refit vs `RiskOutcome` rows; keep current table as fallback below a sample floor | implemented; fallback active | Versioned isotonic persistence, runtime application, API/health status, and `RISK_CONFIDENCE_CALIBRATION_MIN_SAMPLES=100` are implemented; current 0-row train split prevents activation `[verified 2026-10-23]` |
| E4 | Label/outcome pipeline: historical backfill and honest forecast-status acknowledgment | done (2026-10-23) | Offline replay is available; `/data/labels/progress` and Forecast/Data Lake UI show usable samples against the unchanged 30 gate. Live DB has 35 usable samples and 375 forecasts; this replay added 0 due no eligible historical feature/entry timestamps `[verified 2026-10-23]` |
| E5 | Single-writer SQLite wedge → WAL mode + write-serialization queue | done (2026-09-14; re-audited 2026-10-23) | `storage/write_queue.py`, `storage/repository.py`, `engine/run.py`; behavior coverage in `tests/test_write_queue.py` `[verified 2026-10-23]` |
| E6 | LLM: configurable OpenAI-compatible endpoint instead of hardwired Ollama; unpin the 0.5B default | done (2026-10-23) | `llm/engine.py` supports Ollama and OpenAI-compatible chat completions, timeout/retries/circuit breaker, and validated JSON; `tests/test_llm.py` uses respx fixtures `[verified 2026-10-23]` |
| E7 | API authentication and network exposure (loopback defaults; explicit authenticated LAN binds) | done (2026-10-23) | Bearer middleware in `api/main.py:97-166`; `common/network.py` validates API/UI bind settings; GUI API/SSE clients send the configured token; tests cover CORS, startup rejection, defaults, and SSE `[verified 2026-10-23]` |
| E8 | History-divergence guard: pre-push hook blocks non-fast-forward pushes to `refs/heads/main` (`scripts/check_history_divergence.py` via `.pre-commit-config.yaml`, pre-commit env-var contract `PRE_COMMIT_FROM_REF`/`TO_REF`/`REMOTE_BRANCH` + raw-stdin fallback); CI `history-guard` job backstops push events (`--check github.event.before` vs `github.sha`, `fetch-depth: 0`); deliberate rewrites acknowledged via `ALLOW_HISTORY_REWRITE=1` (hook/CI) or `[history-rewrite]` head-commit marker (CI); branch-protection toggles documented in `ci.yml` header | done (2026-09-28; re-audited 2026-10-23) | `scripts/check_history_divergence.py`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, and `tests/test_history_divergence.py` remain present `[verified 2026-10-23]` |
| E9 | Close operational test-coverage gaps | done (2026-10-23) | Behavior tests bring watchdog to 93%, LLM calibration to 96%, liquidity watcher to 81%, validation CLI to 92%, reference seeding to 89%, and `validation.__main__` to 100%; full suite measures 77.71%, with `fail_under=75.7` `[verified 2026-10-23]` |
| E10 | Fresh local startup and launcher API authentication | done (2026-10-25) | `ensure_api_token()` refreshes cached settings after adopting the first-run token; regression in `tests/test_api_token_provisioning.py`; clean-clone smoke proves mode 0600, authorized/unauthorized health, seeded scores, GUI, and launcher URL `[verified 2026-10-25]` |

### N — Whole new modules

| ID | Item | Status | Notes |
|----|------|--------|-------|
| N1 | Solana on-chain safety: mint authority, freeze authority, LP-lock status | done (2026-10-23) | `ingestion/solana_safety.py`, migration `0022_solana_safety_snapshots`, point-in-time feature factory, named risk weights, `/tokens/{id}`, and GUI evidence table; unsupported LP layouts remain unknown `[verified 2026-10-23]` |
| N2 | EVM bytecode/honeypot static analysis (hidden mint, blacklist/pausable, proxy backdoor, fee-on-transfer) | deferred | Dedicated static-analysis module is not present; `crawlers/sources/explorer.py` covers explorer lookups `[verified 2026-10-23]` |
| N3 | Mempool/pending-tx sniper + MEV-bundle detection | deferred | `mempool/` contains burst monitoring, but no sniper/MEV-bundle detector `[verified 2026-10-23]` |
| N4 | Offline historical backfill/replay engine for synthesized labels | done (2026-10-23) | `--replay-labels` runs locally from saved PIT features/prices with bounded forward outcomes and true label observation times; replay is covered by leakage tests `[verified 2026-10-23]` |
| N5 | Funding-source / CEX-deposit wallet-cluster graph tracing | deferred | `fingerprint/engine.py` has wallet-cooccurrence analysis, not funding-source/CEX tracing `[verified 2026-10-23]` |
| N6 | Secrets/credentials management (rotation, at-rest encryption, per-crawler scoping) | deferred | No dedicated secrets-management module; environment settings remain in `common/config.py` `[verified 2026-10-23]` |
| N7 | Portfolio/position paper-trading simulation ledger | deferred | No paper-position ledger module is present `[verified 2026-10-23]` |
| N8 | Point-in-time leakage guardrails: `validation/leakage_guard.py` and feature enforcement | done (re-audited 2026-10-23) | `require_decision_ts` and `check_rows_point_in_time` exist in `validation/leakage_guard.py`; tests and `docs/leakage-audit.md` remain `[verified 2026-10-23]` |

### O — Omissions / reductions

| ID | Item | Status | Notes |
|----|------|--------|-------|
| O1 | Disable Nitter + X-trends crawlers by policy | done (2026-10-23) | Both settings default to `False`; `NIGHTCRAWLER_ENABLE_NITTER` and `NIGHTCRAWLER_ENABLE_X_TRENDS` opt in, with legacy `*_ENABLED` aliases; disabled crawlers appear as `disabled_by_policy` in API/fleet status `[verified 2026-10-23]` |
| O2 | De-emphasize non-benchmarkable score family in the GUI and API | done (2026-10-23) | Six score families carry `score_validation[family].validated=false`; GUI badges link the field report; top pages default to confidence order and offer explicit unvalidated sort options `[verified 2026-10-23]` |
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

### 2026-10-23 — T6: Offline historical label replay (done; no eligible replay rows)

- Added an offline `scripts/backfill_history.py --replay-labels` path and
  `data_lake.labels.replay_historical_labels()`. Entries and feature rows must
  have been observed by their decision time; target prices are bounded to the
  forward horizon and replay as-of time. Label `observed_at` records when the
  full outcome became known. Provider history backfills now record retrieval
  time rather than backdating availability to the historical price timestamp.
- The dense-label endpoint accepts `decision_ts` as an as-of cutoff; API and
  GUI forecast status reports usable matched label pairs against the unchanged
  30-sample gate. Leakage tests cover late entry/target prices and target
  prices outside the forward window.
- Live `serpent.db` before/after: labels 522 → 522; Forecast rows 375 → 375.
  There are 35 usable forecast samples (`ForecastEngine._collect_samples`
  independently returned 35), so the API/UI status is `Forecasting live:
  35/30 labels`. Offline replay found 0 eligible decision points because no
  feature rows were observed by their historical decision times; it added 0.
  The first Forecast rows were already present before this task.
- Versioned live run report: `reports/label-replay-2026-10-23-t6.json`.
- Pre-task checks: pytest 795 tests, 0 failures, 11 skipped; Ruff passed; mypy
  passed (53 files); broken-import scan passed (244 files); declared-deps scan
  passed (28 roots, 244 files).
- Post-task checks: pytest 800 tests, 0 failures, 11 skipped; Ruff passed; mypy
  passed (53 files); broken-import scan passed (244 files); declared-deps scan
  passed (28 roots, 244 files).

### 2026-10-23 — T7: LLM provider abstraction (done)

- Added `LLM_PROVIDER`, `LLM_BASE_URL`, optional `LLM_API_KEY`, `LLM_MODEL`, and
  `LLM_TIMEOUT_SECONDS`, plus bounded retry/circuit-breaker settings. The
  deprecated `LLM_OLLAMA_URL` alias remains effective when the base URL is not
  set. `openai_compat` uses `/v1/chat/completions`, probes `/v1/models`, and
  falls back to `/health` when the models route is unsupported.
- Structured output uses a Pydantic schema; fenced JSON and trailing prose are
  accepted. Invalid output returns empty narrative/risk and zero deltas, which
  the scoring layer skips. Provider request timeouts and tenacity retries bound
  each call; the existing watchdog now bounds the complete optional LLM batch
  so scanning proceeds without its results on timeout.
- Removed the implicit 0.5B model pin from runtime defaults, Compose, installer,
  and install docs. Added `docs/llm-providers.md` with llama-server/OpenAI
  compatible settings and Vulkan/RADV guidance.
- Pre-task checks: full pytest suite passed (800 tests, 11 skipped); Ruff passed;
  mypy passed (53 files); broken-import scan passed (244 files); declared-deps
  scan passed (28 roots, 244 files).
- Post-task checks: pytest 808 passed, 11 skipped; Ruff passed; mypy passed
  (53 files); broken-import scan passed (244 files); declared-deps scan passed
  (28 roots, 244 files).

### 2026-10-23 — T10: Operational coverage gaps (done)

- Added behavior coverage for watchdog timeout-hook isolation, recovery, alert
  caps, disk states, scan counters, and persisted health; LLM calibration
  evaluation, band boundaries, balanced rates, and weight limits; liquidity
  watcher short circuits, malformed events, RPC failure accounting, and
  idempotency; validation CLI argument/exit paths and entry point; and
  reference seed idempotency.
- Focused coverage: `ops/watchdog.py` 93%, `scoring/llm_calibration.py` 96%,
  `radar/liquidity.py` 81%, `validation/cli.py` 92%, `storage/seed.py` 89%,
  `validation/__main__.py` 100%. All exceed the 80% target.
- Full-suite coverage: **77.71%**, up from 74.96% in the task brief; raised
  `fail_under` from 65 to 75.7 (two percentage points below measured coverage).
- Pre-task checks (T9 post-check baseline): pytest 811 passed, 11 skipped;
  Ruff passed; mypy passed (53 files); broken-import scan passed (244 files);
  declared-deps scan passed (28 roots, 244 files).
- Post-task checks: pytest with `--cov=.` 839 passed, 11 skipped, 1
  deprecation warning; total coverage 77.71%, above the 75.7 gate; Ruff and
  formatting passed; mypy passed (53 files); broken-import scan passed
  (246 files); declared-deps scan passed (28 roots, 246 files). Focused tests
  passed (45).

### 2026-10-23 — T9: Social crawler policy gates (done)

- Nitter and X Trends now default off. `NIGHTCRAWLER_ENABLE_NITTER=1` and
  `NIGHTCRAWLER_ENABLE_X_TRENDS=1` opt in; prior `*_ENABLED` environment names
  remain supported. Each enabled source logs one policy override warning per
  process.
- Disabled sources remain in orchestrator status with `disabled_by_policy`;
  the GUI fleet table shows “Disabled by policy” and uses dashes for activity
  metrics. Updated source policy, README, install guidance, and `.env.example`.
- Regression tests cover defaults, status visibility, both env-name forms, and
  one-time warnings without network access.
- Pre-task checks (T8 post-check baseline): pytest 808 passed, 11 skipped;
  Ruff passed; mypy passed (53 files); broken-import scan passed (244 files);
  declared-deps scan passed (28 roots, 244 files).
- Post-task checks: pytest 811 passed, 11 skipped, 1 deprecation warning;
  Ruff passed and 260 files formatted; mypy passed (53 files); broken-import
  scan passed (244 files); declared-deps scan passed (28 roots, 244 files).

### 2026-10-23 — T8: Unvalidated score presentation (done)

- Added `score_validation[family].validated` metadata to API score rows and the
  Confidence Dashboard breakdown for `hype`, `ethos`, `liquidity_access`,
  `manipulation`, `catalyst`, and `research_priority`; all remain `false` until
  they have ground truth.
- Added visible warning badges, linked report tooltips, and per-column
  unvalidated labels in the GUI. The Confidence Dashboard displays the latest
  report's verdict/status cells and leakage cross-check state.
- `/tokens/hot` and `/scores/top` now default to confidence ordering. The Top
  Hype and Top Research pages retain explicit selectors for their unvalidated
  sort keys and offer risk, exit risk, and uncertainty ordering. The score API
  still permits those unvalidated sorts when explicitly requested.
- Regression coverage verifies the six API validation flags, default and
  selected ordering, rejection of unsupported sort keys, and the same metadata
  in token detail and Confidence Dashboard API responses.
- Pre-task checks: pytest 808 passed, 11 skipped; Ruff passed; mypy passed
  (53 files); broken-import scan passed (244 files); declared-deps scan passed
  (28 roots, 244 files).
- Post-task checks: pytest 808 passed, 11 skipped; Ruff passed; mypy passed
  (53 files); broken-import scan passed (244 files); declared-deps scan passed
  (28 roots, 244 files).

### 2026-10-25 — Functional-pass baseline and premise check

- Premise check: the fail-closed API gate (`_RequireApiTokenMiddleware`,
  `_check_bearer_token`) is on `main` (added in `0f02af4`, "restrict binds and
  authenticate UI requests"). `main` is an ancestor of `fix/functional-pass`,
  so this branch already has the gate. The earlier "gate missing" note was based
  on a review branch that is not on this checkout; it does not apply here.
- The working tree already contained uncommitted F7 parity changes in
  `STATE.md`, `ops/parity.py`, and `tests/test_parity.py`; they were retained.
- Fresh venv install completed on Python 3.12.3. Baseline `pytest -q
  -p no:cacheprovider` exited 0 with the existing ignored `serpent.db` present;
  default pytest `addopts=-q` suppressed the final count. Baseline Ruff and
  format checks passed (266 files); current unified mypy passed (161 files);
  import scan passed (249 files); declared-deps scan passed (28 roots, 249
  files). Full count is recorded by the final gate below.

### 2026-10-25 — F4 and F5: validation evidence and honest status

- Ran `python -m validation --self-test --db /tmp/hype-validation-2026-10-25.db
  --out /tmp/hype-validation-2026-10-25.json` against a read-only SQLite backup.
  Self-tests passed. The report measured RiskBand distance 2.202 vs baseline
  1.798 (n=396), BLACK precision 0.0884 vs base rate 0.0884 (n=2,013),
  risk/exit-risk concordance 0.9687 (n=396, suspicious-good), and confidence
  calibration error 0.0798 vs 0.10 (n=2,013). It flags three point-in-time
  features with availability 0.0000. Snapshot counts: 522 labels and 375
  Forecast rows; forecast/hazard/ensemble cells remain insufficient-data.
- Traced the three feature flags: `upsert_feature` sets `Feature.observed_at`
  to `utc_now()` on materialization/refresh; SQL market and liquidity source
  queries filter `observed_at <= decision_ts`, followed by
  `check_rows_point_in_time`. Added a regression test demonstrating the
  materialization timestamp can be later while all input rows obey the cutoff.
  The report finding remains flagged; this does not clear the full persisted
  feature history.
- Added the predictive-edge evidence criteria and explicit forecast, hazard,
  and ensemble unvalidated messaging to label progress and GUI views. The
  `forecast_min_samples=30` and validation flags were not weakened.

### 2026-10-25 — F6–F8: SQLite phases, parity coverage, and types

- Kept per-site `ensure_utc` normalization: a storage-wide `TypeDecorator`
  would change every ORM DateTime result on SQLite and PostgreSQL, while the
  observed defects are boundary-specific. The seeded integration test runs the
  scan, data-lake, retention, and parity phases against file-backed SQLite with
  external source calls stubbed; all phase results are error-free.
- F7: parity records neutral `ok` health and returns `no_data` when the archive
  is empty or contains no point-in-time feature coverage. A populated archive
  divergence still pages red. Empty, uncovered, populated-mismatch, and
  page-on-red tests pass.
- F8's shared Makefile/CI package list remains unified; mypy reports no issues
  across 161 source files.

### 2026-10-25 — F9–F11: docs, hermeticity, and security scan

- Corrected README/INSTALL: fresh `python -m engine` defaults to local-single
  SQLite; `.env.example` is tracked and selects PostgreSQL; `.env` is ignored
  local state. Changed bootstrap help to loopback.
- F10 guard now reads the existing root SQLite fleet directly in read-only
  mode and no longer binds `SessionLocal()`/PostgreSQL. Its root-database suite
  tests passed; a helper regression verifies configured database settings are
  irrelevant.
- F11: external Nitter RSS parsing uses defusedxml; duplicate/MinHash MD5 calls
  declare `usedforsecurity=False`; SSE has finite connect/read/write/pool
  timeouts. The archive reader uses DuckDB's relation API; Bandit reports no
  medium/high findings and no nosec mismatch warnings.

### 2026-10-25 — F2/F3 and final verification

- Fresh-clone smoke first exposed a cache-order bug: module logging populated
  `get_settings()` before `ensure_api_token()` added the token, so `/health`
  returned 403 despite a persisted mode-0600 token. `ensure_api_token()` now
  clears the settings cache whenever a token is activated. A regression test
  failed before the fix and all six token-provisioning tests pass afterward.
- `PATH=/tmp/hype-functional-venv/bin:$PATH ./scripts/dev.sh smoke` passed:
  seeded data-lake/parity phases, authorized `/health` 200, unauthenticated 401,
  seeded scores 200, GUI 200, `data_lake=ok`, parity non-red, direct engine
  SIGINT shutdown, and launcher opening the expected loopback GUI URL followed
  by clean SIGINT shutdown `[verified 2026-10-25]`. External requests were
  isolated by the harness's closed loopback proxy.
- Final full suite: `python -m pytest -o addopts= -q -p no:cacheprovider` —
  858 passed, 11 skipped, 24 warnings. Coverage gate and remaining static gates
  are recorded in the final verification section below.

### 2026-10-25 — F1: data-lake pass crashes on SQLite (done)

- Root cause 1: `_temporal_score` subtracted a naive `observed_at` (read back
  from SQLite) from an aware `decision_ts`, raising `TypeError`.
  Fix: normalize both with `ensure_utc` before subtracting (`data_lake/signal.py`).
- Root cause 2: `run_data_lake_pass` read label progress from `label_counts`,
  an undefined name at that point. Fix: use the `progress` dict that was
  already computed (`data_lake/manager.py`).
- Evidence: `tests/test_data_lake_manager.py` (3 tests). On HEAD without the
  fix the naive/aware test fails with `TypeError: can't subtract offset-naive
  and offset-aware datetimes`; with the fix all 3 pass.

### Final verification — 2026-10-25

- `ruff check . && ruff format --check .`: clean; 268 files formatted.
- Unified mypy package list: 161 source files, no issues.
- `scripts/check_broken_imports.py`: 251 files; `scripts/check_declared_deps.py`:
  29 third-party roots across 251 files.
- Bandit first-party packages at `-ll`: exit 0, no medium/high findings.
- `git diff --check` and `bash -n scripts/dev.sh`: pass.
- `python -m pytest tests/ -q --cov=. --cov-report=term`: total coverage 78.56%;
  the unchanged 75.7% threshold is met.
- Merged live `origin/main` at `1d24499` into the feature branch via merge
  commit `a18ae3b`; it updates only `coverage/history.json` and `coverage/trend.svg`.
  Fast-forwarded `main` and `fix/functional-pass` without rewriting history;
  both remote refs now include the final state-ledger commit and are synchronized.
