# Phase 9 — Benchmark Harness Design

_Status: Stage 2 deliverable. Architecture + self-test plan. No benchmark
execution code written yet (hard gate)._
_Date: 2026-08-25._

This document specifies the benchmark framework (`validation/`) that will
implement the metrics and verdict rules fixed in `docs/validation-methodology.md`.
It also pre-commits the expected results of the three synthetic self-tests so
the implementation is checked against a stated contract, not adjusted after the
fact.

---

## 2.1 Architecture

### 2.1.1 Module layout

```
validation/
  __init__.py
  metrics.py       # pure metric functions: brier, ECE, wilson_ci, concordance,
                   # precision_at_k, ordinal band distance, calibration tables.
                   # No DB access; numpy-only. Unit-testable in isolation.
  baselines.py     # naive baselines: base-rate predictor, random ranking,
                   # liquidity-depth-only heuristic. Pure functions.
  leakage.py       # leakage-suspicion checks (Stage 2.2 case 3 pattern).
  synthetic.py     # synthetic known-answer datasets + the 3 self-tests.
  report.py        # structured, versioned report dataclass + JSON (de)serialization.
  harness.py       # orchestrator: loads point-in-time data from the engine DB,
                   # partitions walk-forward, stratifies by regime, computes
                   # per-cell metrics, emits a ValidationReport.
  cli.py           # `python -m validation --db ...` entrypoint.
tests/
  test_validation_harness.py   # permanent pytest regression for the self-tests.
```

### 2.1.2 Data sourcing — reuse the existing point-in-time architecture

The harness is **read-only** and reuses the engine's own persisted
point-in-time rows. It does **not** build a parallel data path.

- **Scores** (`scores` table): one row per (asset, decision_ts, model_version)
  with the ten `formulas.py` scores + `risk_band`.
- **Outcomes** (`risk_outcomes` table): scored tokens evaluated after the
  observation window; `collapsed` / `rugged` / `survived` flags,
  `lifecycle_phase_at_eval`, `price_change_pct`.
- **Labels** (`labels` table): point-in-time ignition/collapse labels produced
  by `forecast/labels.py` (numeric definitions: peak ≥ +20% → ignition; trough
  ≤ −70% → collapse, within `forecast_forward_hours`).
- **Forecasts** (`forecasts` table): `p_collapse_24h`, `p_ignition_24h`,
  `expected_hours_to_peak/collapse`, `calibration_bucket`.
- **Lifecycle** (`lifecycle_events`): terminal phases for censoring and
  survival ground truth.
- **Market rows**: via `backtest.runner.point_in_time_market_rows` (the same
  `observed_at <= decision_ts` guard the engine uses) for forward-window
  outcome computation and regime classification.
- **Ensemble state** (`ensemble_state.weight_history`, `scorer_accuracy`) and
  **LLM calibration state** (`llm_calibration_state.weight_history`) for the
  meta-metric (2.5 in the methodology doc).

All joins carry explicit `observed_at <= decision_ts` filters. The harness never
writes to the engine's tables — it only reads and writes its own versioned JSON
report.

### 2.1.3 Walk-forward partitioning

- The full scored history `[t0, t1]` is split into **train/reference** and
  **evaluation** by an explicit cutoff date (default: last 30% of the scored
  window, never less than 7 days).
- **Embargo**: the 48h after the cutoff is dropped from the reference set
  (≥ the 24h forward horizon, per methodology §1.1).
- **Purge**: reference samples whose forward label window `[ts, ts+24h)`
  overlaps the evaluation window are dropped from reference metrics.
- Every cell reports `(train_n, eval_n)` and the exact cutoff.

### 2.1.4 Regime stratification

Regimes are deterministic, documented rules computed from the engine's own
aggregated market evidence (methodology §1.4), per evaluation window:

- **Bull**: median cross-asset `one_hour_return` over the trailing 72h > +2%.
- **Bear**: median `one_hour_return` < −2%.
- **Dead**: median 24h volume across scored assets below the 25th percentile of
  the full history **or** median liquidity below the 25th percentile.
- **Mixed**: otherwise.

Where the window is too short or data too sparse to classify, the cell is
`unclassified` and reported as such. Every metric cell is reported per regime
with its own sample size; `insufficient_data` is a valid verdict.

### 2.1.5 Report format (versioned)

`ValidationReport` (dataclass) serialized to JSON with:
- `report_format_version: 1`
- `git_sha`, `model_version`, `generated_at_utc`, `database_digest`
  (counts of assets/scores/forecasts/labels at read time)
- `partition`: cutoff, embargo hours, purge hours
- `cells`: list of `MetricCell` — each with `output` (e.g. `risk_band`,
  `collapse_probability_24h`, `exit_risk`, `hazard_time_to_collapse`, ...),
  `metric` (brier/ece/precision@10/concordance/ordinal_distance/...),
  `regime`, `n`, `value`, `wilson_ci`, `baseline_value`, `baseline_ci`,
  `verdict` (`better_than_baseline` / `indistinguishable_from_baseline` /
  `worse_than_baseline` / `insufficient_data`), `leakage_suspected`.
- `suspicious_results`: list of cells flagged by the leakage detector.

Reports are written to `reports/validation-{timestamp}.json` and are the
canonical run-over-run comparison artifact.

### 2.1.6 Harness self-testability

- All metric functions are pure and numpy-only → unit-testable without a DB.
- The harness's DB-facing loader is thin (row → dataclass); the metric layer
  consumes plain `(prediction, outcome)` vectors so the synthetic self-tests
  exercise the exact same code path as the real run.

---

## 2.2 Self-test plan — pre-committed expectations

Three synthetic datasets with known answers. Expected results are committed
**here**, before implementation. The implementation passes only if it matches
these within the stated tolerances; if it does not, the harness is debugged,
not the expectations.

### Case 1 — Perfect predictor

- Synthetic data: 500 samples, base collapse rate 0.10, a feature `X` that is
  a deterministic function of the outcome (perfect separation).
- **Expected:**
  - `brier` = 0.0 (± 1e-9) — perfect probabilities predict outcomes exactly.
  - `ece` = 0.0 (± 1e-9) — all predictions are 0.0/1.0 and match outcomes.
  - `precision_at_10` = 1.0.
  - `concordance` = 1.0 (rank-AUC).
  - `ordinal_band_distance` = 0.0 (bands map 1:1 to risk scores).
  - `verdict` = `better_than_baseline` (baseline brier = 0.10·0.90 = 0.09).
  - `leakage_suspected` = **False** — perfect *but legitimate* discrimination
    must NOT be confused with leakage.

### Case 2 — Pure random noise at the true base rate

- Synthetic data: 500 samples, base collapse rate 0.10, predictions drawn from
  `Uniform(0,1)` independent of the outcome (seeded for reproducibility).
- **Expected (corrected during Stage 3 — see erratum below):**
  - `brier` ≈ 0.333 = 1/3 (± 0.02). Erratum: the originally committed
    `0.09 = b(1-b)` is the Brier of the *constant base-rate predictor* (a
    different model). For predictions `p ~ Uniform(0,1)` independent of
    `y ~ Bernoulli(b)`, `E[(p−y)²] = E[p²] − 2E[p]E[y] + E[y²] = 1/3 − b + b = 1/3`
    regardless of `b`. Measured 0.3278 — confirms the harness.
  - `verdict` for Brier = `worse_than_baseline` (noise is worse than predicting
    the base rate — the important property is it is *never* reported as better).
  - `ece` ≥ 0.05 (uncalibrated uniform noise over 10 bins: measured ≈ 0.39).
  - `precision_at_10` = `indistinguishable_from_baseline`. Erratum: the
    originally committed numeric band `[0.03, 0.17]` assumed the top-10 draw
    lands near the base rate, but with k=10 and b=0.1 a random draw yields 0
    positives with probability `0.9^10 ≈ 0.35`. The correct assertion is the
    *verdict*: the point estimate (here 0.0) falls inside the Wilson CI of the
    base rate for a k=10 draw, so the verdict is `indistinguishable_from_baseline`.
  - `concordance` ≈ 0.50 (± 0.10).
  - `verdict` = `indistinguishable_from_baseline` on ranking metrics (never
    `better_than_baseline`).
  - `leakage_suspected` = False.

### Case 3 — Deliberately injected leakage

- Synthetic data: 500 samples; the outcome `y` is computed, then a feature
  `L` is created as `y` copied into a feature *observed after* the decision
  time (simulating the exact failure mode from the leakage audit: a value only
  knowable post-decision, e.g. a future-dated feature or a label-derived
  column). The model "predicts" using `L`.
- **Expected:**
  - Raw metrics are perfect (`concordance` = 1.0, `brier` ≈ 0) — this is the
    trap the detector must catch.
  - `leakage_suspected` = **True**, because the harness's point-in-time check
    (feature `observed_at <= decision_ts`) fails for `L`.
  - The cell is moved to `suspicious_results` and its verdict is recorded as
    `leakage_suspected` — **never** reported as a legitimately great score.
  - A second, legitimate feature `X` (observed before decision) with partial
    discrimination is NOT flagged.

The leakage check itself is: for the top-3 features by concordance, verify all
non-missing values were observed at or before the decision time; any violation
with near-perfect discrimination (concordance ≥ 0.95) ⇒ `leakage_suspected`.
This mirrors the leakage-audit remediation rule the engine already applies to
its own features (`observed_at <= decision_ts`).

---

## 2.3 Deliverable for this stage

This document. Stage 3 (build + self-test) implements `validation/` exactly per
§2.1 and must pass the §2.2 expectations before touching real engine data.

### Erratum — Stage 3 corrections to §2.2 Case 2 (documented reasoning errors)

The phase spec permits correcting a pre-committed expectation when a documented
reasoning error is found. During Stage 3 self-testing, two such errors were
found in the Case 2 (noise) expectations and corrected with justification
inline above:

1. **Brier of uniform noise.** The committed `0.09 = b(1-b)` is the Brier of the
   constant base-rate predictor, not of random `Uniform(0,1)` predictions. The
   correct value is 1/3 (derivation above; measured 0.3278).
2. **Precision@10 band.** The numeric band `[0.03, 0.17]` assumed the top-10
   random draw lands near the base rate; with k=10 the draw has ~35% probability
   of containing zero positives (precision 0.0). The correct assertion is the
   verdict (`indistinguishable_from_baseline`), not a tight numeric band.

No expectation was changed to match code output: the corrected values are
closed-form statistics, and the measured values (0.3278, 0.0) match the
corrections. The other two cases' expectations were unchanged.
