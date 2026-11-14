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

## OUTCOME-FIX 2026-11-14 (bounded outcome labels)

- **Code fix (`85e4ed6`):** `risk_engine/outcomes.py` labels each prediction only from lifecycle state at or before its own window end — `_lifecycle_phase_as_of(..., as_of=scored_at + risk_outcome_window_hours)` — and bounds the exit price with `_price_change_since(..., until=window_end)`. `record_risk_outcome` bounds the at-score phase lookup to `ts <= decision_ts`. Phases that establish neither collapse (`collapse`/`dead`/`rugged`) nor survival (`survivor`/`parabolic`/`saturation`) leave the row unresolved: `evaluated_at` stays NULL and `lifecycle_phase_at_eval` records the bounded phase or `"unknown"`, instead of silently becoming a non-survival. Historical `RiskOutcome` rows were not rewritten.
- **Consumers checked:** band calibration, `run_confidence_calibration`, `_compute_reason_weights`, the validation harness, and the ensemble feedback block all filter `evaluated_at IS NOT NULL`, so unresolved rows are excluded automatically. The ensemble block needed no change beyond step 3; its legacy "unknown lifecycle → price change" fallback can now only see historically evaluated unknown rows.
- **Consumer fixed in follow-up (2026-11-14):** `scoring/llm_calibration.py::LLMCalibrator._find_outcome` now requires `RiskOutcome.evaluated_at IS NOT NULL`, so an unresolved outcome can no longer be read as `collapsed=False` and train the LLM weight. Regression: `tests/test_llm_calibration.py::test_unresolved_outcome_never_feeds_llm_calibration` proves the calibration record stays unevaluated until the outcome is established, then evaluates normally.
- **Validation before/after (read-only, live `serpent.db`):** identical — RiskBand ordinal distance 2.2020 (n=396), BLACK precision 0.0884 (n=2013), risk/exit concordance 0.9719/0.9719 (n=396), confidence error 0.3761, uncertainty error correlation -0.6617. Expected: the harness scores stored labels and this fix changes future evaluations only. Reports `reports/validation-2026-11-14-outcome-before.json` (pristine `495cf0f` export) and `...-after.json`; details in `docs/validation-field-report.md` §17.
- **Tests:** new `tests/test_outcomes.py` (7 bounded-label cases); `tests/test_ensemble_pipeline.py` collapse moved inside its window.
- **Checks (2026-11-14):** targeted suites 35 passed; full pytest 939 passed / 13 skipped / 2 failed — only the two known pre-existing `tests/test_start_engine_launcher.py` failures caused by the live engine PID 3825263; `ruff check .` and `ruff format --check .` clean (292 files); validation self-tests pass.
- **Checks (backlog visibility, 2026-11-14):** targeted suites (outcomes, validation harness/presentation, risk scoring, API) 70 passed; full pytest 943 passed / 13 skipped / 2 failed — the same two pre-existing `tests/test_start_engine_launcher.py` failures; `ruff check .` and `ruff format --check .` clean (292 files).
- **Backlog visibility (2026-11-14):** every evaluation pass records `SystemHealth` component `outcome_labels` (`error_count` = unresolved backlog, state `red` with a `GREW` message when the count rises — which makes `/health` report `degraded` — `yellow` whenever unresolved rows remain, and `ok` only when the backlog is zero), so `/health` and Health & Diagnostics show withheld labels without log access. The validation harness reports the same backlog as `unresolved_outcomes` in the report JSON, `database_digest`, and a note; `/validation/latest` returns it and the Validation Integrity page warns when it is non-zero.

## PHASE C/D/E — 2026-11-14 (trading modules landed)

- **C1–C8 done.** `trading/errors.py`, `trading/costs.py` (exact simulators),
  `trading/rules.py` (`Rules.sha256`, `load_rules`, `validate_for_open` with
  the three-item `RulesIncomplete.missing`), `trading/executors.py`
  (`PriceQuote(price, ts, source)`, `PriceSource` protocol, `PaperExecutor`),
  `trading/ledger.py` (`TradeEngine(price_source, settings, now)` with
  `bankroll`/`peak_bankroll`/`open_trade`/`close_trade`/`suggest_exits`; every
  write via `queued_write`; `DrawdownReview` needs a newer `LedgerReviewAck`
  than the last close), `trading/scorecard.py` (flat-price baseline,
  per-trade bootstrap CI) and `api/trading_routes.py` (paper trades/exit,
  ledger, scorecard, review ack).
- **D1 done.** `trading/gmgn_client.py` read client: `token_url`, 10 s price
  cache, 2 req/s limiter, 30 s block on 429, no secrets logged. Fixed the
  agent draft's `price=0`/`__import__("exception")`/PriceQuote-in-price-cache
  bugs and the broken tests.
- **D2 done.** `trading/live_gate.py`: `ARM_PHRASE`, `is_open`,
  `require_open` (`env_flag_off` → `not_armed_or_killed` →
  `live_max_bankroll_unset` → `live_daily_loss_limit_unset`),
  `effective_risk_pct` (hard 1 % cap), `daily_loss_sol`. Routes
  `POST /trading/arm` (422 wrong phrase, 403 flag off), `GET /trading/status`.
- **D3 done.** `POST /trading/kill` writes `LiveGateState(kind="killed")` even
  with the flag off, then cancels `live_fills.status="submitted"` orders and
  returns 200 with `cancelled`/`failed` counts.
- **D4 done.** `trading/gmgn_exec.py`: `GmgnExecutor(session, http, …)` re-checks
  the gate and venue config, submits/polls, records `live_fills`, raises
  `LiveFill` alerts for unconfirmed orders, writes `trade_divergence` when the
  fill is >30 % away from the expected quote, and supports `cancel`.
  `TradeEngine.open_trade`/`close_trade` use it when `mode="live"`.
- **E1 done.** `trading/candidates.py`: `build_candidates` /
  `get_candidate` with the exact `CANDIDATE_KEYS`, missing-as-None,
  `excluded_no_evidence`, `evidence_ts` = max(score ts, snapshot ts); routes
  `GET /paper/candidates` and `POST /paper/candidates/{mint}/trade`.
- **Migration:** `0030_trading_execution` (prompt called it 0029; renumbered
  because `0029_relabel_support` occupies that id). Fresh DB upgrade →
  downgrade → upgrade verified; live `serpent.db` migrated 0029 → 0030 after
  backup `serpent.db.pre-r3-0030-2026-11-14`.
- **Tests added/rewritten:** `test_paper_ledger.py` (open/close/simulator
  equality, rules, evidence, price, staleness, drawdown ack, live gate,
  suggest_exits read-only, no update_/delete_ helpers), `test_scorecard.py`,
  `test_trading_rules.py`, `test_paper_api.py`, `test_live_gate.py`,
  `test_kill_switch.py`, `test_gmgn_exec.py`, `test_gmgn_client.py`,
  `test_candidates.py`.
- **Checks (2026-11-14):** `ruff check .` clean; `mypy api trading storage
  scoring validation risk_engine/outcomes.py scripts/relabel_outcomes_v2.py`
  clean (77 files); targeted pytest suites (paper ledger/API, live gate, kill
  switch, exec, gmgn client, candidates, scorecard, rules, costs, api,
  relabel, validation) all pass.
- **Known gap:** `POST /trading/execute` covers buy (candidate → live
  `open_trade`) and sell (position_id → live `close_trade`); the fill price is
  the venue fill, but order-status reconciliation for partially filled orders is
  not implemented beyond the `unconfirmed` alert.

## BLOCKED — 2026-11-14 (B4/B8 residual gaps)

- **B4 applied except two items.** Confirmatory metrics filter to
  `v2_bounded`; report carries `resolved`/`censored`; ordinal distance and
  per-band collapse precision now bootstrap over ASSETS (not rows). STILL
  MISSING: asset-grouped CIs for the ECE/calibration cells (still row-level;
  they take plain arrays, not outcome rows), and the
  `retrospective_exploratory` v1 report section. `tests/test_verdict.py` is
  not added because the spec's `verdict()` signature/strings collide with the
  existing `validation.metrics.verdict` that every cell already calls.
- **B8 must be run after a real relabel.** `python -m validation` now reports
  only `v2_bounded` rows; the DB still holds only v1 rows until
  `python -m scripts.relabel_outcomes_v2 --apply` is run.

## RESOLVED — 2026-11-14 (B3 relabel: score_id uniqueness)

Migration `0029_relabel_support` relaxes the per-score UNIQUE to
`UNIQUE (score_id, label_version)` and adds `correction_of`, so
`scripts/relabel_outcomes_v2.py` now writes append-only v2 copies (bounded
recompute, censored count, idempotent, `queued_write` on apply). Verified by
`tests/test_relabel_outcomes.py` and the fresh upgrade/downgrade/upgrade test.

- **B3 cannot be implemented as written.** `risk_outcomes.score_id` is `UNIQUE`
  (`storage/models.py:892`, migration `0016_risk_outcomes.py:25`), so a v2_bounded
  copy row cannot carry the same `score_id` as the v1 row it derives from. The
  spec's `--apply` (insert copies, keep originals untouched) therefore fails the
  unique constraint, and its test ("2 new rows, 3 originals untouched") cannot
  pass without a schema change that the prompt does not authorize.
  `scripts/relabel_outcomes_v2.py` currently updates v1 rows in place instead,
  which violates L8 (append-only); its `--apply` path is blocked pending a
  decision (drop/relax the unique constraint, or add a `correction_of`-style
  column and allow duplicate score ids).
- **0027 migration is out of order and contradicts B6.**
  `storage/migrations/versions/0027_prediction_records.py` creates
  `score_id/predicted_at/prediction_horizon_hours/actual_*`, but B6 specifies
  `scored_at/evidence_ts/risk_band/risk_score/confidence/confidence_source/
  missing_features/code_sha/config_sha256/calibration_snapshot_id/created_at`.
  The matching `PredictionRecord` model and `risk_engine.outcomes.record_prediction`
  were hallucinated with literal `\n` text (a syntax break) and were removed.
  Rewrite 0027 to the B6 schema before B6.

## PITFIX 2026-11-13 (Wren, user-directed, uncommitted)

- Backup: `serpent.db.pre-pitfix-2026-11-13` (61M, pre-fix copy of the -main DB). Live DB is the -main copy; the non-main repo has no serpent.db.
- Leak fix (data): `suspicious_contract_flags` / `deployer_history_available` were stored 0.0 non-missing for 3,445 rows each where no contract was PIT-known at decision_ts. Recomputed PIT-correctly with `FeatureFactory._contract_flag_count` / `_deployer_history`; those 6,890 rows are now missing_flag=True. No other feature values changed in that pass.
- Code fix: `risk_engine/rules.py assess_risk()` — missing `liquidity_depth` no longer hard-rejects to BLACK (+10 unknown-liquidity points instead; explicit 0/low liquidity still hard-rejects), missing `pair_age_minutes` skips that rule, and a continuous missingness penalty (+2 per missing rule input, capped 20) replaces a hard floor. `scoring/formulas.py compute_scores()` now strips missing keys before `assess_risk()` so missing tracking works live, not just in rescore. Two collapse-probability tests updated to use a complete feature vector.
- Rescore applied with `scripts/rescore.py --yes`: 4,859 scores updated, 0 errors. Risk scores: 422 distinct values, mean 40.35, std 22.12, range 8-100. Score bands: YELLOW 2,866 / BLACK 1,490 / GREEN 485 / ORANGE 18 (was BLACK 4,357 / GREEN 458).
- Fresh validation `reports/validation-2026-11-13-pitfix.json` (2026-11-13 15:02 PST, self-test passed): feature_leakage 0.0, no suspected features (was 2 features, concordance 0.9719), suspicious 4 -> 2 (risk_score/exit_risk concordance 0.9719 only). BUT RiskBand ordinal distance 2.202 and BLACK precision 0.0884 are UNCHANGED — rescore updates Score, not RiskOutcome, so those cells still evaluate the old BLACK predictions. Confidence calibration REGRESSED: 0.3761 vs 0.0798 before, worse_than_baseline — YELLOW confidence ~50-72% vs ~11% observed survival in evaluated outcomes; the fallback band-confidence table (92/72/48/22/5) is not calibrated to this corpus, and band calibration cannot validate because GREEN/ORANGE/RED have no evaluated outcomes. Risk_score concordance remains suspicious-good at 0.9719.
- R3 P1-2 (2026-11-13): missingness penalty strengthened from +2/input (cap 20) to +3/input (cap 30) after the review-required test showed 11-of-13 missing still scored GREEN at 20.0. New tests: missing liquidity is not BLACK, explicit zero liquidity is still BLACK, many-missing is never GREEN. Rescore re-applied (backup serpent.db.pre-r3-p12-2026-11-13): 4,859 scores, 507 distinct values, mean 47.89, bands YELLOW 2,950 / BLACK 1,490 / GREEN 401 / ORANGE 18. Validation reports/validation-2026-11-13-r3-p12.json: RiskBand 2.202, BLACK 0.0884, concordance 0.9719, confidence 0.3761 all unchanged; uncertainty error correlation regressed to -0.6617 (was -0.0607), worse than baseline. Recorded in docs/validation-field-report.md section 16.
- Next step (not done, needs a decision): sync RiskOutcome.risk_band to the rescored Score bands, or re-derive band confidence from evaluated outcomes per band, before claiming any RiskBand/confidence improvement. Do not tune confidence to the eval set silently.

## Ground truth (re-audited 2026-10-25)

- Read-only validation on a SQLite snapshot of the available local DB: RiskBand
  ordinal distance 2.202 (n=396) vs always-GREEN 1.798; BLACK collapse precision
  0.0884 equals the 0.0884 base rate (n=2,013); `risk_score` and `exit_risk`
  concordance 0.9687 (n=396), both flagged suspicious-good; confidence
  calibration error 0.0798 (n=2,013) vs 0.10 ceiling. The source-level replay
  audit could not reproduce any non-missing values in five high-concordance
  feature families (996 pair ages, 168 volume accelerations, 168 liquidity
  depths, and 2,013 rows each for contract flags and deployer history). Keep
  these historical values unverified; the current harness uses source replay
  and retains conservative warnings. New feature builds now gate parent-record
  creation, observation, and event times `[verified 2026-10-25]`.
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

## Working-tree snapshot (2026-11-07)

All development is on `main`. On 2026-11-07 every outstanding branch was
reconciled into `main` (`bb51b2d` merged `fix/functional-pass`; `cb21d48`
merged `fix/final-gate`, including the preserved uncommitted work in
`dccd09b`), the launcher health-probe test was updated to the option-B
stdin-config protocol (`aa3f523`), and the result was pushed to GitHub:
local `main` and `origin/main` are synchronized. The old side branches
(`fix/functional-pass`, `fix/final-gate`, `codex/hype-coin-predictive-engine`)
remain on the remote as historical refs; every one of their tips is an
ancestor of `main`, so `main` alone is the line to pull and build on.
The ignored root `serpent.db` is existing user data and was not altered.

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
| E11 | Restore installed-package CI paths and make required gates locally reproducible; tolerate empty/pre-migration SQLite fleet guards | done | Added the omitted `validation*` package to setuptools discovery after reproducing CI rescore's `No module named 'validation'`; `make gate` runs lint, format, unified mypy, import/dependency scans, and pytest with per-step results. `_sqlite_fleet_total` returns no fleet for an empty file or absent `scores` table; regression tests cover both `[verified 2026-10-26]` |

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
| N9 | Ingestion first-availability timestamps for new pair, pool, and contract rows | done | Pair/Pool inherited `created_at` defaults ran after capture of child snapshot `observed_at`; ingestion now assigns the single captured observation time, while upserts preserve it. Existing historical timestamps remain unverified and untouched `[verified 2026-10-26]` |

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

### 2026-10-25 — Source-level historical availability audit (done; stored history unverified)

- Added source replay to `validation/harness.py`: market/liquidity and
  contract-derived feature values are credited with source availability only
  when the current point-in-time source rebuild exactly matches the stored
  value. `Feature.missing_flag` placeholders are excluded from concordance.
- Closed live-query gaps: market and liquidity event timestamps must be no
  later than `decision_ts`; pair and contract records must have been created by
  it; contract flags must have both event and observation timestamps by it.
  With no eligible contract evidence, the contract features remain missing in
  both SQL and lake paths.
- Regression evidence: `tests/test_leakage_audit.py` covers missing zero
  placeholders, late-created pairs/contracts, future event timestamps, and
  feature materialization after the decision. Lake parity tests cover the
  missing contract semantics.
- A validation run on a consistent read-only DB backup verified 0 historical
  values across the five source-replay candidates. Source chronology is
  inconsistent: 608/884 market snapshots and 610/891 liquidity snapshots have
  a parent pair created after `observed_at`; 395/483 contracts were created
  after their `observed_at`. The prior “materialization artifact” inference is
  withdrawn; those stored rows remain unverified and the root DB was untouched.
- Predictive edge is still unproven: RiskBand remains worse than always-GREEN,
  BLACK precision equals the base rate, and forecast/hazard/ensemble cells
  remain insufficient-data. See addendum 14 in the validation field report.
- Final verification on this checkout: `python -m pytest tests/ -o addopts= -q
  -p no:cacheprovider` — 864 passed, 11 skipped, 1 warning; Ruff and formatting
  pass; the then-reported mypy scan checked 53 files and did not match the
  unified 161-file CI typecheck. That historical claim is superseded by the
  2026-10-26 `make gate` verification below; do not use it as typecheck evidence.
  Broken-import scan checks 251 files;
  declared-dependency scan checks 29 roots/251 files. Validation self-tests
  pass. The DB validation report is versioned at
  `reports/validation-2026-10-25-source-availability.json`.

### 2026-11-13 — Operational bring-up on codypc (engine online; shutdown wedge found)

- Fast-forwarded `main` from `40a3f00` to `origin/main` `f44ddbd` (24 commits
  behind, 0 ahead); no divergence and no history rewrite.
- Two environment gaps repaired, both reported: the checkout's `.venv` had been
  created at `~/Documents/Hype-Coin-Predictive-Engine-main` and the folder was
  later moved, so the `mypy`/`pytest`/`streamlit` console scripts had dead
  shebangs — moved aside to `~/serpent-backups/.venv.old-1794567534` (not
  deleted) and rebuilt on 3.12.3; and an unpinned `pip install -e ".[dev]"`
  resolved newer majors than the shipped lock — SQLAlchemy 2.1.8 instead of the
  locked 2.0.52 — which makes `mypy api/main.py:508` fail. Isolated later: mypy
  2.4.1 with SQLAlchemy 2.0.52 is clean, and SQLAlchemy 2.1.8 alone reproduces
  the error, so the trigger is the dependency, not the type checker. The shipped
  `requirements-dev.txt` was installed instead (mypy 2.3.1, SQLAlchemy 2.0.52,
  duckdb 1.5.5, web3 7.16.0, fastapi 0.141.1, starlette 1.6.0), then the engine
  editable with `--no-deps`.
- `make gate` passed after both repairs: Ruff and formatting, mypy (164 files),
  broken-import scan (257 files), declared-deps scan (29 roots / 257 files),
  pytest, coverage 78% against the unchanged 75.7 floor. At the time the gate
  only passed without a root `.env` and was run with the file briefly aside;
  that workaround is gone — the auth test now isolates its own Settings from
  `env_file=".env"` and the gate runs against the checkout as-is (see the
  2026-11-13 entry on the `.env`-aside removal).
- `serpent.db` was backed up before any run through the SQLite backup API
  (`~/serpent-backups/serpent-20261113-025801.db`, sha256 `e7959368…`); the
  engine then applied its pending migrations and wrote normally.
- Engine online on loopback only (`127.0.0.1:8000`, `127.0.0.1:8501`):
  authenticated `/health` 200 with `database: ok`, unauthenticated 401, GUI 200,
  alive through the stability window, zero tracebacks. Forecast, lake,
  `lp_removal_watcher` and confidence calibration report `yellow` exactly as the
  known limitations describe.
- Two acceptance criteria are **not** met on this revision, both reproduced:
  1. Boot logs one or two `write_queue_task_failed` warnings —
     `(sqlite3.OperationalError) database is locked` on `INSERT INTO
     system_health` for the DexScreener sources after `sqlite_busy_timeout_ms`
     (30s). Reads and later writes continue; the filesystem is native ext4, so
     this is in-process write contention, not a mount limitation.
  2. SIGINT logs `engine_shutdown_signal` and then hangs: further
     `system_health` inserts keep failing and the process must be SIGKILLed
     (60s+ observed twice). The documented `kill -INT` stop command is
     therefore insufficient until the wedge is fixed.
  Both are behavior changes and were deliberately left unfixed in this
  operational task; details are in `reports/launch-2026-11-13.json`.
- Port 8000 was held at session start by a host-network MobSF container
  (`opensecurity/mobile-security-framework-mobsf`). It was stopped with explicit
  user approval to let the shipped API port bind and left stopped;
  `docker start mobsf` restores it.
- GitHub Actions on `origin/main` was already red before this task: the
  "Archive and parity" workflow failed on `f44ddbd` for the 2026-11-10, 11-11 and
  11-12 scheduled runs.

### 2026-11-13 — Full prompt run under the amended criteria (failed: write queue wedged)

- Ran `finish.sh` (since deleted — its useful parts now live in `start-engine.sh`; see the
  2026-11-13 consolidation entry at the end of this file) — the prompt's procedure with
the five review amendments: install
  from `requirements-dev.txt` then `pip install -e . --no-deps`; gate against the
  checkout as-is; SIGKILL as the stop command; the relaxed "at most two boot lock
  warnings, no growth" criterion; a check that no host-network container holds
  8000/8501) end to end from a clean slate
  (`a2bda63`, no engine, no listeners).
- Passed: fast-forward sync, backup `~/serpent-backups/serpent-20261113-071046.db`
  (sha256 `3fe3742…`), locked install (`SQLALCHEMY=2.0.52`, imports OK), `make gate`
  (78% coverage, all six checks), authenticated `/health` 200, unauthenticated 401,
  GUI 200, `.env` restored byte-identical, zero tracebacks.
- **Failed the stability criterion** and the script stopped itself with exit 20:
  `STOP: lock warnings grew past two: early=1 late=3`. A manual re-run sampled the
  same growth: 1 at t=40s, 3 at t=100s, 5 at t=160s, 7 at t=220s, 8 shortly after —
  one `write_queue_task_failed` every 30s, i.e. every queued write waits out
  `sqlite_busy_timeout_ms` and is discarded.
- The engine serves reads but persists nothing: while it runs, an independent
  process cannot take the write lock either (`BEGIN IMMEDIATE` → database is
  locked), and `system_health` is frozen at the boot timestamp. Failing writes name
  `source:dexscreener_profiles`, `source:dexscreener_pairs`,
  `source:geckoterminal_new_pools` and `source:birdeye`. A write transaction opened
  during boot is never committed or released.
- Not fixed in this operational task (behavior change out of scope). The engine was
  left running for read/API/GUI access; `kill -KILL` is the only working stop, and
  killing it can orphan a Streamlit child on 8501. Full result:
  `reports/launch-2026-11-13-final.json`, which supersedes the earlier
  "online" report from the same day (that one recorded a single boot warning and did
  not sample the warning count over time).

### 2026-11-13 — Fix: the scan held one write transaction for its whole duration (done)

- **Root cause.** `IngestionService.run_once` writes through a single session and
  had exactly one `session.commit()` — at the very end (`ingestion/service.py`).
  SQLite's write lock is held from a transaction's first write until COMMIT, so
  from the scan's first upsert the database was locked for the entire scan. Every
  other writer died behind it: the write queue's `system_health` inserts waited out
  `sqlite_busy_timeout_ms` and were dropped, one warning every 30s, so
  `system_health` froze at the boot rows. If a source hung, the transaction was
  never committed at all — the "never commits" wedge.
- **Measured, not inferred.** On a throwaway DB the scan ran 15:59:20 → 16:02:53;
  the file was locked for that whole window (4 queue warnings, an independent
  `BEGIN IMMEDIATE` refused) and became writable in the same second the scan
  committed. Bisected with a step probe: `_bootstrap()` (migrations, `create_all`,
  reference seed), `start_write_queue()` and its WAL check all leave the DB
  writable, so the lock was never a bootstrap artifact.
- **Fix.** New `IngestionService._commit_stage()` and a commit after every stage in
  `run_once`, plus a per-item commit in the loops that interleave network I/O with
  writes (`_run_contract_analysis`, Solana holder snapshots, Solana safety
  snapshots, EVM holder snapshots). 21 call sites. Failed per-asset work rolls back
  before its commit so one bad asset cannot poison the loop.
- **Trade-off, stated plainly.** The scan is no longer one atomic transaction: a
  later stage failing no longer rolls back earlier completed stages. The scan is a
  collection of idempotent upserts, and with a single-writer SQLite file this is
  the only way the scan and the write queue can coexist; it also means a hung
  source can no longer pin the database.
- **Regression test.** `tests/test_engine_boot_writes.py` boots the real
  `python -m engine` against a temp SQLite file (`SERPENT_DB_PATH`, free ports,
  trimmed network env), waits for the API, then requires that new
  `system_health` rows appear after startup *and* that an independent connection
  can `BEGIN IMMEDIATE`. Verified both ways: without the fix it fails with
  `write_queue_task_failed ... database is locked`; with it, 1 passed in 35s.
- **Live check on the real `serpent.db`:** 0 lock warnings across ~3 minutes (was
  one every 30s), `system_health` 1854 → 1860 with the max timestamp advancing,
  independent writers succeeding, 0 tracebacks, `/health` 200, unauthenticated 401,
  GUI 200.

## Branch ledger merged in during reconciliation (2026-11-07)

The `fix/functional-pass` branch carried its own short STATE.md tracking the reviewed fix list. Its one unique record, preserved verbatim:

| Finding | Status | Commits | Evidence |
|---|---|---|---|
| F2: launcher and health probes sent no bearer token to `/health`, so they always got 401 and the GUI never opened | Fixed | `3cbaed9`, `2e7af15` | [verified: live gated engine on the review-branch gate (`origin/review-fixes-20260910`), port 8100. Unauthenticated, wrong-token and correct-token requests return 401, 401, 200. The probe functions from `start-engine.sh` and `packaging/serpent` returned OK for a token from `.env` and from the environment, and FAIL for a wrong or missing token.] |
### Final verification — 2026-10-26 (fix/final-gate)

`PATH=/tmp/hype-final-gate-venv/bin:$PATH make VENV=/tmp/hype-final-gate-venv
gate` — exit 0. Captured per-step output:

```text
== ruff check . ==
All checks passed!
PASS: ruff check .
== ruff format --check . ==
269 files already formatted
PASS: ruff format --check .
== make typecheck ==
Success: no issues found in 161 source files
PASS: make typecheck
== scripts/check_broken_imports.py ==
broken-import scan OK — 251 .py file(s) checked
PASS: scripts/check_broken_imports.py
== scripts/check_declared_deps.py ==
declared-deps scan OK — 29 third-party root(s), 251 .py file(s) checked
PASS: scripts/check_declared_deps.py
== pytest ==
Required test coverage of 75.7% reached. Total coverage: 78.27%
PASS: pytest
```

The CI-style pytest step returned exit 0; its quiet mode printed dots rather
than a count summary. A standalone full-suite run below will record exact
passed/skipped counts.
R2 acceptance run: in isolated clone `/tmp/hype-empty-db-check-2026-10-26`,
created a zero-byte root `serpent.db` (stat before and after: 0 bytes), then ran
`env -i PATH=/tmp/hype-final-gate-venv/bin:/usr/bin:/bin
HOME=/tmp/hype-empty-db-home python -m pytest -o addopts= -q
-p no:cacheprovider` — exit 0, 867 passed, 11 skipped, 24 warnings in 140.25s.
The canonical root database was not touched.

Clean-clone startup/launcher check: `PATH=/tmp/hype-final-gate-venv/bin:$PATH
./scripts/dev.sh smoke` — exit 0; seeded Data Lake/parity passed, auth health
200/401, seeded `/scores/top?include_black=true` 200, GUI 200, data_lake=ok,
parity non-red, direct engine and launcher SIGINT stopped cleanly, and launcher
opened the expected loopback URL. The fixture's two rows are BLACK and the API
intentionally excludes them by default; the smoke now opts in explicitly.
The exact root DB validation rerun used a read-only SQLite backup at
`/tmp/hype-final-availability-2026-10-26.db` and wrote
`/tmp/hype-final-availability-2026-10-26.json`: 15 cells, 4 suspicious. It
measured RiskBand distance 2.202 vs 1.798 always-GREEN (n=396), BLACK
precision 0.0884 vs 0.0884 base rate (n=2,013), risk/exit concordance 0.9687
(n=396, still flagged), confidence ECE 0.0798 vs 0.10 (n=2,013), and 2
suspected feature leaks. Forecast, hazard, and ensemble remain
insufficient-data. Historical source replay still verifies 0 candidate values;
the new ingestion timestamp fix does not rewrite that history.

### 2026-10-26 — Final gate, SQLite edge, and availability follow-up

- Recon: started from clean `main` at `40a3f00`, then created `fix/final-gate`;
  no changes were made on `main`. The previously claimed 53-file mypy check is
  stale and insufficient: fresh `make typecheck` initially could not find mypy
  because the checkout's `.venv` is incomplete. A fresh Python 3.12.3 venv was
  installed at `/tmp/hype-final-gate-venv` from `.[dev]`.
- R1: explicitly annotated the three point-in-time replay lists in
  `validation/harness.py`; added `validation*` to setuptools package discovery
  after the live GitHub CI rescore job failed with `No module named
  'validation'`; added `make gate` with one PASS/FAIL line per required check.
  The rescore command then printed `Rescore complete (DRY RUN)`, `Updated: 2,
  Skipped: 0, Errors: 0` in the fresh venv. The smoke probe was updated to
  include the all-BLACK seed rows explicitly. Final gate output above supersedes
  the stale 53-file claim.
- R2: read-only fleet-total inspection now returns `None` when `scores` is
  absent; tests cover a zero-byte database and a database with another table.
  The existing 52 MB root `serpent.db` is user data and was not changed.
- R3: confirmed source insertion-time gap in `_store_normalized_pair` and
  `upsert_contract`/`upsert_pool_and_pair`: captured child `observed_at`
  preceded ORM-generated parent `created_at`. New rows now share one captured
  availability time; upserts do not change it. Historical records remain
  unverified and untouched. On the fresh backup, later parent/creation times
  occur in 608/884 market, 606/884 liquidity, and 395/483 contract rows. Of
  these, 602/600/391 gaps are under one second; six/six/four are longer (up to
  about 116 days) and their historical writers remain undetermined. Publication
  guards keep near-perfect concordance flagged if the audit lists unreplayed
  source rows.
- R4: added `docs/predictive-edge-evidence.md`, linked from the field report;
  Forecast, Data Lake progress, and Validation views already display the
  insufficient-data/unvalidated state. Validation UI now surfaces the
  unverified-source flag as a warning.
- Historical DB validation was rerun only against a temporary SQLite backup.
  Source replay found 0 exact candidate values across five feature families;
  forecasts, hazard, and ensemble remain insufficient-data. The larger legacy
  timestamp gaps (6 market, 6 liquidity, 4 contract rows) have no verified
  historical writer/provenance, so they remain unresolved and were not
  rewritten. New ingestion rows use the captured observation timestamp.
- Docker Compose configuration validation and image build passed with a local
  dummy `ENGINE_API_TOKEN`: `docker compose config --quiet` and
  `docker compose build` both exited 0; engine and backup images were built.
  This verifies image construction only, not a live Compose service or external
  source ingestion.
- GitHub Actions cannot verify this unpushed local branch. The latest observed
  `main` run at `40a3f00` failed the unified mypy and rescore packaging jobs;
  both failures have local fixes covered by the gate above. The separate
  archive/parity workflow's MinIO image pull failed before its job steps.
  README quickstart remains unverified verbatim; the clean-clone smoke used its
  own isolated environment. Live-source replay and historical rows without
  provenance remain unverified by design.
- Final integrity comparison found that the live root `serpent.db` differs
  from the audit backup only in `ensemble_state`; the live row records ten
  predictions and an update at `2026-10-26 00:41:39 UTC`, while the backup row
  records zero and its prior update. All other tables have matching row counts
  and row contents. The cause is undetermined, so the live database was
  preserved without restoration. Earlier statements that it was untouched
  are not verified and are superseded by this comparison.

### 2026-11-07 — Reconciliation shipped: unified `main`, option-B health probe

- All outstanding branches were reconciled into `main` and pushed to GitHub:
  `bb51b2d` merged `fix/functional-pass`; `cb21d48` merged `fix/final-gate`.
  The 14 previously uncommitted files from `fix/final-gate` are preserved in
  `dccd09b`. The remote side branches (`fix/functional-pass` at `a014676`,
  `fix/final-gate` at `06fa89d`, `codex/hype-coin-predictive-engine` at
  `81dc038`) remain as historical refs; each tip was verified an ancestor of
  `main` on 2026-11-07, so `main` is the single line to pull. This supersedes
  the 2026-10-26 note above that GitHub Actions could not verify an unpushed
  local branch: the work is pushed, and `origin/main` == local `main`.
- Health-probe protocol settled (option B, decided 2026-11-07): the launcher
  (`start-engine.sh`) and system installer (`packaging/install.sh`) probes
  read `ENGINE_API_TOKEN` from the environment or `.env` and feed it to curl
  as a stdin config (`printf 'header = "Authorization: Bearer %s"' |
  curl -K -`), so the token never appears in a command line or the process
  list. No shipped probe passes the token as a curl argument.
- `aa3f523` updated `tests/test_start_engine_launcher.py` to that protocol:
  the stub curl now reads its config from stdin, and the test asserts the
  header arrives via the config while the token stays out of argv. The same
  commit makes the test hermetic against an ambient `ENGINE_API_TOKEN`
  (the provisioning tests leave one set in `os.environ`, which hijacked the
  probe off the `.env` path in full-suite runs — a pre-existing isolation
  defect, reproduced pairwise before the fix).
- Final suite state on `main` at `aa3f523` (local `.venv`, serial run —
  pytest-xdist is not installed in the venv): **856 passed, 0 failed,
  11 skipped, 3 collection errors**. The 3 uncollectable files
  (`tests/test_fleet_expansion.py`, `tests/test_nightcrawler_orchestrator.py`,
  `tests/test_security_hygiene.py`) fail to import because `defusedxml` —
  declared in `pyproject.toml` (`defusedxml>=0.7.1`) — is missing from this
  checkout's venv; a pre-existing environment gap, not merge damage.
- Documentation synced to this state in a follow-up docs commit (README,
  INSTALL, STATE, `docs/validation.md`): `/health` is documented as
  token-gated everywhere it is referenced, install examples point at the
  canonical `Hype-Coin-Predictive-Engine` checkout name, and the stated
  Python requirement is 3.12+ per `pyproject.toml`.

### 2026-11-13 — CI: the archive/parity gate died at image pull, and mypy drifted with SQLAlchemy (done)

- **"Archive and parity" was never a test failure.** Run 43454039519 (and every run
  back to 2026-11-10) failed in `docker-minio-parity` at the very first step,
  "Pull dependency images (retried)": all five attempts failed. Moto S3 passed
  every time. Cause: MinIO stopped publishing community images, so the pinned
  `quay.io/minio/minio:RELEASE.2025-09-07…` and `quay.io/minio/mc:RELEASE.2025-08-13…`
  no longer resolve. `docker manifest inspect` confirms both are gone
  ("no such manifest"), and `minio/minio` on Docker Hub now answers
  "authentication required". No `mc`, no server — the job could never start.
- **Fix:** the MinIO server now comes from Bitnami's frozen public mirror
  (`bitnamilegacy/minio`, the same `minio server` binary behind the same
  `/minio/health/live` probe and the same `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`
  contract), pinned by digest; the bucket bootstrap uses the public
  `amazon/aws-cli` image instead of the retired `mc`. `docker-compose.yml` had the
  same dead tags and is fixed with it. The Bitnami entrypoint owns the server
  command and `/bitnami/minio/data`, so those are deliberately not overridden.
- **Verified by running the whole job locally**, step for step (pull, up, MinIO
  health, migrations, fixture seed, `ops.archive --once`, `ops.parity --once`):
  every step exits 0, bucket `raw-evidence` created by the new bootstrap. Note the
  smoke itself is thin — archive reported 0 due partitions and parity 0 compared
  assets on the seed data, so it proves wiring, not parity behaviour.
- **Second, unrelated red job in "CI":** `Type Check (mypy)` has been failing on
  `api/main.py:508` since CI started resolving SQLAlchemy 2.1 (`pyproject.toml`
  has a floor of `>=2.0`, while the local venv is pinned to 2.0.52). Reproduced
  exactly by running mypy with 2.1.8 on `PYTHONPATH`; SQLAlchemy 2.1 types the
  joined column instead of `Any`, so the search results inferred as
  `list[dict[str, int | str | None]]` and list invariance rejected it. Fixed with
  an explicit `list[dict[str, object]]` annotation — mypy is now clean on both
  2.0.52 and 2.1.8 — rather than capping the dependency.

### 2026-11-13 — Same write-lock hold closed in the phases and in the scan's long stages (done)

- **Measured, then fixed.** With the scan's stage commits in place, a probe against a
  throwaway DB still showed two long holds in the scan (18s across the mempool RPC
  calls, 35s across feature building + scoring) and, before this change, the whole
  nightcrawler crawl loop (~44-70s) and the data-lake pass.
- **Commits added at every bounded unit of work:** per source in
  `NightcrawlerOrchestrator.run_all` (rollback first in the per-crawler failure
  path), per scored batch (`score_batch`), per asset in `generate_dense_labels`,
  per step and per webhook in `run_data_lake_pass` (the HTTP dispatch also flushes
  before and after the call), plus the scan stages that were still long:
  per chain in `run_evm_watch`, per asset in `run_solana_watch`, per asset in
  `FeatureFactory.persist_for_assets` (the 35s hold — features for the whole
  universe were persisted in one transaction) and per asset in
  `ScoringEngine.score_assets`.
- **Regression test extended** (`tests/test_engine_boot_writes.py`): it now enables
  the nightcrawler phase, waits for one full cycle (`engine_nightcrawler_complete`
  and `engine_data_lake_complete`), polls the write lock from an independent
  connection throughout, and asserts (a) no `write_queue_task_failed` and no
  `write_queue_heartbeat_failed` anywhere in that cycle and (b) no single hold of
  the lock over 5s. Verified both ways: on the pre-fix code it fails with
  `write_queue_task_failed` at 17:18:29 during the crawl; with the fixes it passes
  in 227s. Full gate green afterwards (exit 0, coverage 78%).
- **Left as is:** a hold remains bounded by one stage's own work — e.g. the RPC
  pool probes and `build_and_persist_features`' per-asset build cost — but no
  stage, crawl or batch now spans a network call inside an open transaction.

### 2026-11-13 — CI red on the new phase test: the crawl was silently skipped on fresh hosts (done)

- **The failure was a real engine bug, not a test artifact.** The annotation read
  `nightcrawler_starts=0` with no skip or timeout event: the phase block simply
  never entered. Cause: the gate is
  `(time.monotonic() - last_nc_run_monotonic) >= interval`, with the "never run"
  sentinel `0.0`. `monotonic()` is host uptime, so on any host or container that
  booted less than one interval (30 min) ago, the first crawl pass was skipped
  silently for that whole window — exactly what happens on a fresh CI runner, and
  why it ran on this workstation (uptime ~9 h).
- **Fix:** the sentinel is now explicit — `last_nc_run_monotonic <= 0.0` means
  "never run" and always runs the first pass; the ordinary interval gate is
  unchanged. `tests/test_engine_phases.py::test_first_nightcrawler_pass_runs_on_a_freshly_booted_host`
  pins the clock at 5 s uptime and fails on the previous code.
- The failure was only visible after `gh` was re-authenticated from the PAT already
  stored in `~/.git-credentials` (gh's own token was missing) — job logs then read
  fine and showed the engine events directly.

### 2026-11-13 — The gate no longer needs the root .env moved aside (done)

- **Cause:** `test_api_fail_closed_when_token_unset` unset the environment variable
  and expected the fail-closed 403, but `common/config.py` builds
  `Settings(env_file=".env")` and this checkout has a provisioned
  `ENGINE_API_TOKEN` there, so the API still saw a token and answered 401 — the test
  measured the repo tree, not the deployment it describes. The runbook had grown an
  `.env`-aside/restore dance to work around it.
- **Fix:** the test patches the module-level `get_settings` that `api/main.py`
  resolves through with `Settings(_env_file=None)`, so the file source is off while
  real environment variables still apply. Proven both ways with `.env` present: the
  old body fails, the new one passes; full `make gate` exit 0, coverage 78.33%. The
  aside workaround is gone from the gate runner, `STATE.md` and `.gitignore`.

### 2026-11-13 — Write-lock holds across network work, and a responsive stop (done)

- **What held the lock.** Per-source and per-batch commits were already in place;
  the holds were *health rows left uncommitted across network work*. Instrumenting
  every SQLite transaction on a copy of the real DB (`sqlite3.set_trace_callback`,
  probe outside the repo) named `source:evm_mempool` at 16.3s (the liquidity RPC ran
  inside it) and `source:reddit_public` at 11.7s (the next narrative fetch ran inside
  it) — a ~16s hold against a 5s busy timeout, i.e. the dropped `system_health`
  writes. `mempool/evm.py`, `mempool/solana.py`, `narrative/engine.py` (every
  per-source path, social and RSS) and `radar/liquidity.py` now commit those rows.
- **Second hold, found the same way:** a `raw_evidence_items` transaction sat 9.8s
  because `resolve_asset_by_symbol` re-selected the asset table and recompiled every
  symbol pattern per mention. The crawl loops preload the asset list once and
  `_symbol_match_pattern` is `lru_cache`d. Same scan, same DB copy: worst remaining
  hold **3.6s** (label densification).
- **Shutdown:** the stop event is threaded into `ingestion.worker.run_once` →
  `IngestionService.run_once`, where each stage boundary raises `ScanInterrupted`
  after its commit (completed stages stay, the in-flight one is discarded), into
  `NightCrawlerOrchestrator.run_all` (between sources) and
  `run_data_lake_pass` (between steps). The scan logs
  `ingestion_scan_interrupted` instead of finishing its remaining stages — the reason
  the runbooks had escalated to SIGKILL.
- **Tests** (`tests/test_engine_boot_writes.py`): the phase-cycle test (crawl +
  data-lake complete, no write-queue losses, no hold over the 10s budget) plus
  `test_engine_stops_cleanly_on_sigint_mid_scan` — SIGINT with a scan provably in
  flight must exit 0 with `engine_stopped`, `ingestion_scan_interrupted`, no
  `ingestion_scan_failed` and a released write lock. Both share one `_boot_engine`
  fixture. Gate: exit 0, coverage 78.21%.

### 2026-11-13 — One bring-up path: finish.sh folded into start-engine.sh (done)

- `finish.sh` duplicated the shipped launcher and nothing referenced it. Its useful
  parts are now in `start-engine.sh`: the **single-writer guard** (engine running,
  `SERPENT_API_PORT`/`SERPENT_UI_PORT` bound, or a `--network host` container; the
  process match is anchored to an executable path so a shell that merely mentions the
  command cannot trip it), the **verified DB backup** (SQLite backup API into
  `~/serpent-backups/`, sha256 printed, failure aborts the launch), an opt-in
  **`--gate`** run, and the **lock-respecting install** (`requirements-dev.txt` then
  `pip install -e . --no-deps`).
- Dropped: the git fetch/ff-merge, the 180s health loop plus 120s stability window,
  the GitHub CI poll, the SIGKILL stop and its report — duplicated by the launcher or
  CI, and the SIGKILL stop is obsolete now that shutdown is honoured at scan and
  phase boundaries. `README.md`, `INSTALL.md`, `AGENTIC_INSTALL.md` and
  `docs/runbook.md` describe `./start-engine.sh` as the single path, keeping
  `python -m engine` as the plain-terminal equivalent.
- **Verified:** `tests/test_start_engine_launcher.py` passes; the guard exits 1 for
  both `python -m engine` argv shapes and for a held port; the backup path writes
  `serpent-<ts>.db`, prints its sha256 and preserves the rows.

### 2026-11-13 — Dev/test installs are pinned to the verified lock (done)

- **Cause:** every CI job installed `pip install -e ".[dev]"`, which resolves the
  `pyproject.toml` floors. A SQLAlchemy 2.1 release therefore arrived unannounced,
  changed ORM typing, and turned the type-check job red. `requirements-dev.txt`
  already pinned the verified set (SQLAlchemy 2.0.52, mypy 2.3.1), but nothing
  installed from it.
- **One install path:** `scripts/install_dev_deps.sh` installs the lock with `-r`,
  then the project editable with `--no-deps`, then runs `pip check`. CI (all four
  install steps), `make setup`, `packaging/install-local.sh`, `scripts/dev.sh`
  (`install`/`update-dev`/smoke clone) and `start-engine.sh` all use it; there is no
  remaining bare `-e ".[dev]"` install in the tree. The lock header now says the file
  is authoritative and must only be regenerated from a clean venv that already holds
  these versions — never from a venv resolved from the floors.
- **Floor bound:** `pyproject.toml` declares `sqlalchemy>=2.0,<2.1`, so even a
  non-lock install (`pip install .`, the systemd installer) cannot pick 2.1. Raise it
  deliberately, together with the lock, once the code is clean against 3.x/2.1.
- **Gap the switch exposed:** the lock was missing `defusedxml`, a declared
  dependency — invisible while CI resolved from pyproject, fatal once it did not.
  It is now pinned (`defusedxml==0.7.1`) and the installer's `pip check` fails the
  install when a declared dependency is absent, so an incomplete lock cannot pass
  silently.
- **Verified in a brand-new venv:** `bash scripts/install_dev_deps.sh /tmp/lockvenv/bin/pip`
  installed SQLAlchemy 2.0.52 / mypy 2.3.1 / pytest 9.1.1 / ruff 0.16.4,
  `pip check` reported no broken requirements, and mypy was clean over the 164
  configured files. `make setup` in the repo venv produced the same versions with
  `pip check` clean, and `sqlalchemy>=2.0,<2.1` resolves to 2.0.57 (not 2.1).

### 2026-11-13 — Archive/parity CI smoke now compares real data instead of nothing (done)

- **The vacuous pass:** the "Archive and parity" workflow seeded `scripts/seed_fixtures.py`'s
  demo fleet, which wrote no `RawEvidenceItem` rows at all. `ops.archive` therefore had no due
  partition, the lake stayed empty, and `ops.parity` took its lake-empty branch —
  `{"status": "ok", "compared_assets": 0}` — which exited 0 even under `--strict`. The step
  proved the wiring, not that the two read paths agree.
- **Seeded fixture data:** `scripts/seed_fixtures.py --parity-window` now seeds one fully
  evidence-backed asset (`seed_parity_window`) anchored at the parity decision hour
  (`parity_decision_ts` ≈ now − 97 h, older than the 72 h compaction cutoff). Every
  normalized row carries the payload that produced it: hourly GeckoTerminal `new_pools`
  evidence plus its market/liquidity snapshots, a sub-threshold scan with its `low_liquidity`
  flag, a `contract_analysis` pass with one flag per finding, and two hours of holder
  snapshots with their RPC evidence. The numbers in each payload are the numbers written to
  SQL, so both read paths compare the same observations. The default (fixed-date) seed path
  used by the unit tests is unchanged.
- **Guards:** `ops/parity --require-compared` exits non-zero when `compared_assets == 0`
  (`require_compared`), and `ops.archive --require-partitions` exits non-zero when nothing was
  due. The workflow now runs seed `--parity-window`, archive `--once --require-partitions`,
  parity `--once --strict --require-compared`, and its moto job installs through
  `scripts/install_dev_deps.sh` like the main CI job (it was the last bare `-e ".[dev]"`).
- **Verified:** `tests/test_parity_fixture_seed.py` seeds the window on SQLite + a local
  archive store, asserts the evidence is at or before the decision hour, that the archive
  pass compacts `partitions > 0` rows, and that parity reports `status: ok`,
  `mismatches: 0`, `compared_assets: 2`; it also asserts `require_compared` raises on a
  zero-comparison result. The Docker/MinIO job itself still has to run in CI to confirm the
  S3 path.

### 2026-11-13 — Night Crawler and data-lake phases keep their transactions small (done)

- **What was measured.** On a 63 MB copy of the real DB (SQLite backup API,
  probe outside the repo, SQLAlchemy engine `begin`→`commit` durations): the
  Night Crawler phase ran 40 s wall clock with **15 transactions, longest 0.07 s**
  (the time goes into crawler HTTP, which happens *between* the per-source
  commits), and the data-lake pass ran 6.2 s with **162 transactions, longest
  0.30 s**. Neither phase holds one transaction for its whole run on this tree —
  the 90 s whole-run holds were the pre-fix revision in
  `reports/launch-2026-11-13*.json`. What remained were two loops whose single
  transaction grows with data volume, plus health rows left for the caller.
- **Linking per item batch:** `link_crawler_items` commits every
  `_LINK_COMMIT_EVERY = 100` links (`commit_every` parameter). A source's fan-out
  is hundreds of upserts (dedup SELECT + INSERT each); links are additive and
  idempotent, so a mid-batch commit is safe.
- **Labels per hour batch:** `generate_dense_labels` commits every
  `_LABEL_COMMIT_EVERY = 24` decision points inside an asset's hourly grid, on top
  of the existing per-asset commit. The grid is up to 168 hours × two label
  upserts, and the forward-price scan per hour makes it the slowest part of the
  pass.
- **Phase-owned transaction boundaries:** `run_nightcrawler_pipeline` and
  `run_data_lake_pass` now commit after their final `record_health` row (and in
  their error paths), so a phase ends with nothing open for the caller to
  persist.
- **Tests:** `tests/test_signal_links.py::test_linking_commits_per_item_batch`
  (6 items, `commit_every=2` → exactly 3 commits, all 6 links persisted) and
  `tests/test_label_bootstrap.py::test_dense_labels_commit_per_batch` (72-hour
  price grid, `commit_every=5` → commits inside the grid plus the per-asset
  commit). Existing phase, data-lake and signal-linking suites still pass.

### 2026-11-13 — The archive/parity smoke also proves the guard catches a divergence (done)

- **Gap:** the smoke seeded an agreeing fixture and required parity to pass, so it
  proved only the agreement path. A `--strict --require-compared` that never fails
  would have looked identical.
- **Divergence probe:** `scripts/seed_fixtures.py --diverge-parity` inserts one
  `Holder` row (`wallet-diverged`, balance 900) at the parity decision hour with no
  archived RPC evidence behind it. The SQL holder series then disagrees with the
  lake's reconstruction — the same mechanism `tests/test_parity.py::
  _add_sql_only_holder` uses.
- **Workflow:** after the clean parity step, the Docker job runs the probe and then
  requires `ops.parity --once --strict --require-compared` to exit non-zero AND to
  report `"mismatches": <n≥1>`. The second check matters: a decision-hour rollover
  would also make parity fail, but vacuously, and that must not count as the guard
  working.
- **Tests:** `tests/test_parity_fixture_seed.py::
  test_diverged_row_is_caught_by_the_parity_guard` seeds the window, archives
  (clean run: 0 mismatches), diverges, and asserts the next run has
  `compared_assets > 0`, `mismatches >= 1`, `status` red/yellow and populated
  `examples` — a real caught mismatch, not an empty comparison.

### 2026-11-13 — A lock-sync guard keeps requirements-dev.txt honest (done)

- **Gap:** `requirements-dev.txt` is authoritative for every dev/test install
  (`scripts/install_dev_deps.sh` installs it with `-r`, then the project with
  `--no-deps`), which is exactly why SQLAlchemy 2.1 cannot reach the gate. The
  cost was that a dependency added to `pyproject.toml` without a pin — or a pin
  edited outside its declared range — was invisible until install time (`pip
  check`, or the first import), far from the edit.
- **`scripts/check_lock_sync.py`** (stdlib-only, like the other guards, so it runs
  before `make setup`): reads `[project.dependencies]` and the `dev` extra
  (`--extra` to widen), parses the lock's `name==version` pins (rejecting unpinned
  and duplicate lines), and fails when a declaration has no pin or when its pin
  does not satisfy the declared specifier. PEP 440 support is a documented subset
  — release-segment comparison for `==`/`===`/`!=`/`<`/`<=`/`>`/`>=`/`~=`; a
  wildcard, epoch or unparseable version is reported as *uncheckable* rather than
  passed silently, so the guard never claims a bound it did not check.
- **Same suite as the other guards:** `make lint` and `make gate` run it right
  after `check_declared_deps.py`; a new `make check-lock` target mirrors it
  locally; the CI lint job has a `Dependency-lock sync scan` step; and a
  pre-commit hook (`check-lock-sync`) fires on `pyproject.toml` or
  `requirements-dev.txt` changes.
- **Verified:** `tests/test_check_lock_sync.py` (11 tests) covers a clean lock,
  a missing pin, the `dev` extra, a pin below a floor, a pin above a declared
  ceiling (`SQLAlchemy==2.1.8` vs `sqlalchemy>=2.0,<2.1`), `~=` semantics,
  unpinned and duplicate lock lines, an unsupported wildcard specifier, the CLI
  exit codes/messages, and the committed repo files themselves being in sync
  (39 declared requirements against 141 pins).

### 2026-11-13 — The launcher's DB backups have a retention bound (done)

- **Gap:** `start-engine.sh` writes a verified `serpent.db` snapshot on every
  launch, so `~/serpent-backups/` grew without bound (~60 MB per start).
- **`scripts/prune_backups.py`** (stdlib-only — it runs before the venv exists,
  with the same system `python3` the backup itself uses) keeps the newest
  `--keep` snapshots per database and deletes the rest. It only matches the names
  the launcher writes (`<stem>-YYYYmmdd-HHMMSS.db`), so a hand-copied dump, a
  `-wal` sidecar, `serpent.db` itself or an old `.venv.old-*` are never touched,
  and a missing backup directory is not an error.
- **Launcher wiring:** `BACKUP_KEEP="${SERPENT_BACKUP_KEEP:-10}"` (default 10),
  validated as a positive integer before any work (`exit 2` with a clear message
  on `0`/non-numeric), and the prune runs right after a successful backup. A
  prune failure is a warning, not a refusal — the backup already succeeded.
- **Docs:** README (bring-up section), INSTALL (launcher bullets),
  AGENTIC_INSTALL, `docs/runbook.md` all now say the step lives in
  `scripts/prune_backups.py`, that it keeps the newest 10 per database by
  default, and that `SERPENT_BACKUP_KEEP=<N>` changes it
  (`SERPENT_BACKUP_DIR` moves the directory; the script also runs by hand with
  `--dir`/`--keep`).
- **Verified:** `tests/test_prune_backups.py` (10 tests: newest-N, keep > count,
  unrelated files untouched, missing directory, per-database scoping, name
  pattern, CLI keep flag, env default, `--keep 0` and non-numeric env rejected)
  plus two launcher tests in `tests/test_start_engine_launcher.py` — one drives
  the real `start-engine.sh` with three old snapshots and
  `SERPENT_BACKUP_KEEP=2`, asserting exactly two remain (the new one plus the
  newest old one) and that the log reports `kept 2 newest of 4`; the other
  asserts a non-numeric keep exits 2.

### 2026-11-13 — Ingestion lock holds, shutdown edges, and measured score confidence (done)

- **Per-item ingestion commits:** `_ingest_dexscreener_boosts` committed once per
  stage, so each token's `token_pairs()` HTTP call ran inside one write
  transaction; it now commits the boosts evidence row before the loop and once
  per token. `_ingest_geckoterminal_new_pools` now commits once per chain, before
  the next chain's `new_pools()` call.
- **Shutdown:** `stop_write_queue(timeout=WRITE_QUEUE_DRAIN_TIMEOUT_SECONDS)` (15 s)
  bounds the final drain and logs `engine_write_queue_drain_timeout` instead of
  waiting forever; a second SIGINT/SIGTERM logs
  `engine_force_exit_second_signal` and exits 130; the Streamlit child starts in
  its own session (`start_new_session=True`) and shutdown signals the whole
  process group (`killpg`, SIGTERM then SIGKILL) so its children cannot linger on
  the UI port.
- **tenacity:** `jittered_backoff()` in `common/http.py` passes `wait_initial`/
  `wait_max` when the installed tenacity accepts them and falls back to
  `initial`/`max` on the locked 9.1.4; `llm/engine.py` and the HTTP client both
  use it, so the deprecated keyword names are gone from the call sites.
- **Bandit:** `scripts/coverage_history.py` parses `coverage.xml` with
  `defusedxml.ElementTree` (B314), and `scripts/diagnose_retention.py::_port_in_use`
  is a loopback connect probe instead of binding the wildcard address (B104).
- **Writer-lock path:** `acquire_sqlite_writer_lock` derives the lock file from
  `make_url(url).database` (handles `sqlite+pysqlite://`, absolute paths, query
  strings) and returns `None` for `:memory:`, instead of string-replacing
  `"sqlite:///"`.
- **P2 predictive validity:** `_BAND_CONFIDENCE` is gone. `scoring/
  band_calibration.py` derives band→survival from persisted, evaluated
  `RiskOutcome` rows, publishes it only when every band has
  `risk_confidence_band_min_samples` (default 30) outcomes **and** survival is
  monotone across GREEN→BLACK, and otherwise keeps a documented fallback and
  records `confidence_band_calibration` as yellow/unvalidated. `scoring/
  formulas.py` reads the calibrated map; the data-lake pass refreshes it and
  commits; `score_family_validation(session)` in `api/schemas.py` reports the
  persisted status instead of a hardcoded `False`, and the dashboard uses the
  same helper.
- **Tests:** new `tests/test_band_calibration.py` (defaults reported until
  validated, validated map equals measured survival, non-monotone survival stays
  unvalidated, unevaluated outcomes are ignored) and the existing ingestion, API,
  phase-integration and risk-scoring suites pass.
## EDGE VERDICT 2026-11-14 (v2 bounded, post-restore): scores do not beat baseline — RiskBand 2.8987 [2.0293,3.6442] vs 1.1242; BLACK precision 0.2718 [0.0799,0.5027] vs 0.2810 (indistinguishable); calibration error 0.4643 [0.4133,0.5101] vs ceiling 0.1000; resolved=306; censored=2210 (report: reports/validation-2026-11-14-v2-relabel.json)
