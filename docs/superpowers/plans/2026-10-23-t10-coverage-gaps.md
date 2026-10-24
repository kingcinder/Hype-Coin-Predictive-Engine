# T10 Operational Coverage Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise behavior coverage of six operational modules to at least 80% and set the repository coverage floor two points below the resulting measured total.

**Architecture:** Keep production behavior unchanged unless a behavior test exposes a defect. Add assertions against watchdog recovery/alert caps, LLM-calibration edge handling, liquidity watcher branches, validation CLI entry points, and repeatable reference seeding. Measure all six modules and the repository total with the existing pytest-cov configuration before changing `fail_under`.

**Tech Stack:** Python 3.12, pytest, pytest-cov, SQLAlchemy, existing project fixtures.

## Global Constraints

- Keep `forecast_min_samples` at 30 and `fail_under` at or above 65 until the final measured total is known.
- Tests must not touch the live network.
- Do not add line-only tests; assert externally observable behavior.
- Preserve project mission and point-in-time contracts.
- Commit one conventional commit for T10 after all required checks pass.

---

### Task 1: Watchdog timeout, recovery, and re-alert caps

**Files:** `tests/test_watchdog.py`; production code only if a behavior defect is exposed in `ops/watchdog.py`.

- [x] Add tests asserting timeout hook failure does not escape, a released abandoned stage is tracked as recovered, and skip re-alerting obeys count and duration caps and resets for a new episode; also cover scan recovery, disk states, and watchdog health reporting.
- [x] Run targeted tests; existing behavior passed without a production-code change.

### Task 2: LLM calibration edge behavior

**Files:** `tests/test_llm_calibration.py`; production code only if a behavior defect is exposed in `scoring/llm_calibration.py`.

- [x] Add database-backed assertions for prediction evaluation, risk-band boundaries, balanced-rate stability, and weight floor/ceiling behavior; existing tests cover disabled/insufficient-sample behavior.
- [x] Run the complete calibration test module (22 passed).

### Task 3: Liquidity watcher branches

**Files:** `tests/test_liquidity.py`; production code only if a behavior defect is exposed in `radar/liquidity.py`.

- [x] Assert disabled/missing-source short circuits, malformed and unrelated event rejection, duplicate event idempotency, and per-chain RPC failure accounting/health state.
- [x] Use a monkeypatched RPC seam; no network calls.
- [x] Run the targeted tests (6 passed).

### Task 4: Validation CLI and module entry point

**Files:** new `tests/test_validation_cli.py`; production code only if a behavior defect is exposed in `validation/cli.py` or `validation/__main__.py`.

- [x] Assert missing required arguments return 1 with help, self-test success/failure return codes, output-path forwarding, and module entry-point exit propagation.
- [x] Stub the harness/database boundary; no live database opened.
- [x] Run the targeted tests (4 passed).

### Task 5: Reference seed idempotency

**Files:** new `tests/test_seed.py`; production code only if a behavior defect is exposed in `storage/seed.py`.

- [x] Call `seed_reference_data()` twice using the isolated test database and assert chain, source, and venue counts and identities remain stable.
- [x] Run the targeted test (1 passed).

### Task 6: Coverage gate and final verification

**Files:** `pyproject.toml`, `STATE.md`, and this plan document.

- [x] Measure all targets at >=80% and full repository coverage at 77.71%.
- [x] Set `fail_under` to 75.7%, two points below the measured total and above 65.
- [x] Run the five required project checks and record summary lines and coverage numbers in `STATE.md`. Commit T10 after final review.
