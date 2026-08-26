# Phase 9 — Validation Methodology

_Status: Stage 1 deliverable (hard gate for Stages 2–4)._
_Author: Serpent Circle engineering. Date: 2026-08-25._

This document defines, before any benchmark code is written, how the engine's
predictions will be evaluated: which metrics are used and why, what "ground
truth" means precisely, and what would count as evidence that the system's
predictions are worth trusting. It is written against the engine as it exists
on `main` (post-Phase-0 leakage remediation, post-Phase-4 backtest pipeline).

---

## 1. Literature and practice review

The goal of this section is not to list every evaluation idea that exists, but
to record what the mature literatures (quant finance, ML evaluation,
biostatistics/survival analysis) agree on, so the methodology below adopts
established practice rather than rediscovering it.

### 1.1 Walk-forward / purged / embargoed cross-validation

- **Why naive k-fold is invalid for time series.** Random k-fold shuffles
  observations across the train/test boundary, so training samples whose
  outcome windows overlap test samples leak the future into training. This is
  widely documented in financial ML practice: López de Prado's *Advances in
  Financial Machine Learning* (2018, Wiley) devotes chapters to the problem and
  to the "purged k-fold" remedy; Bailey, Borwein, López de Prado & Zhu (2014,
  *Notices of the AMS*, "Pseudo-Mathematics and Financial Charlatanism") show
  that naive backtest selection over random splits produces systematically
  overfit results.
- **The standard alternatives:**
  - **Walk-forward (anchored/rolling) validation**: train on data up to time
    `t`, test on `(t, t+h]`, roll forward. This is the canonical approach for
    financial forecasting and is what the engine's forecast layer already uses
    (chronological 70/30 split with a purge).
  - **Purging**: remove training samples whose *label windows* overlap the test
    period. Because this engine's labels are computed over a forward window
    (`forecast_forward_hours = 24`), any training sample whose `[ts, ts+24h)`
    window touches the test start must be dropped from training.
  - **Embargoing**: additionally drop a buffer of observations immediately
    after the test period from training, to kill serial-correlation leakage
    from features that use trailing windows (e.g. 7-day velocities). Standard
    guidance (López de Prado 2018) is an embargo at least as long as the label
    horizon; for this system the embargo is the 24h forward horizon.
- **Decision adopted:** walk-forward partitions with purge ≥ 24h and embargo ≥
  24h, matching the engine's existing forecast split discipline, and explicitly
  separating the *evaluation* of the rules/ensemble (which is a measurement of
  the production path) from the *training* of the ML layer (which already
  enforces purge internally).

### 1.2 Calibration measurement for probabilistic forecasts

- The system outputs probabilities and probability-derived risk bands, so
  accuracy/precision alone is insufficient. Standard practice (see e.g. Gneiting
  & Raftery 2007, *JASA*, "Strictly Proper Scoring Rules") treats **proper
  scoring rules** as the primary calibration instrument:
  - **Brier score** (Brier 1950, *Monthly Weather Review* 78(1):1–3): mean
    squared error between forecast probability and outcome. Lower is better;
    a model predicting the base rate everywhere scores `p(1-p)`.
  - **Reliability diagrams** (forecast probability on the x-axis vs. observed
    frequency on the y-axis): the visual statement of calibration.
  - **Expected Calibration Error (ECE)** (Naeini, Cooper & Hauskrecht 2015,
    "Obtaining Well Calibrated Probabilities Using Bayesian Binning into
    Quantiles", AAAI; popularized by Guo et al. 2017, "On Calibration of Modern
    Neural Networks", ICML): the weighted mean absolute gap between predicted
    probability and observed frequency per bin. This is the scalar used by the
    engine's own drift checks (`forecast.calibration_error` uses a 10-bin
    variant).
- **Decision adopted:** Brier + ECE (10 equal-width bins) + reliability
  histograms for every probability output; **Wilson 95% confidence intervals**
  (Wilson 1927) on every observed frequency so small samples cannot masquerade
  as calibrated.

### 1.3 Precision@K and ranking-based evaluation

- The engine's own headline metric is `precision_at_10`: of the 10 tokens
  ranked most likely to collapse (or ignite), what fraction actually did. This
  is a legitimate and standard *ranking* metric — top-K precision is common in
  information retrieval and financial signal evaluation (it measures "if I act
  on the top K flags, how often am I right").
- Known limitations that justify *supplementing*, not replacing, it:
  - Precision@K is threshold-dependent and silent about the rest of the
    distribution; two models with identical precision@10 can differ wildly in
    calibration or full-rank quality.
  - It needs a baseline: on a corpus with base collapse rate `b`, a random
    ranker achieves expected precision@K ≈ `b`. The engine must be compared
    against that floor, not read in isolation.
  - **Decision adopted:** keep precision@10, add precision@5/@25, add the
    **concordance index / rank-AUC** (probability that a randomly chosen
    positive is ranked above a randomly chosen negative — the standard ranking
    summary for both classification and survival models), and report the naive
    precision@K floor beside it.

### 1.4 Regime-change and non-stationarity handling

- Crypto markets are non-stationary: bull/bear/dead regimes shift the base
  rates of ignition and collapse, so aggregate metrics measured across regimes
  are misleading. Standard econometric practice (Hamilton 1989, *Econometrica*
  57(2):357–384, Markov regime-switching; and the extensive HMM application
  literature in crypto, e.g. the survey-style treatments in MDPI *Mathematics*
  13(10):1577 and Preprints 202603.0831) treats regime as a latent state that
  changes the return distribution.
- **Decision adopted:** stratify every headline metric by a deterministic,
  documented regime rule computed from the engine's own aggregated market
  evidence (see Stage 2 design), and report per-regime base rates alongside
  per-regime metrics. Where a regime stratum has too few samples to support a
  Wilson CI wider than the configured tolerance, report `insufficient_data`
  rather than an aggregate that hides the gap.

### 1.5 Known failure modes specific to meme-coin / pump-and-dump prediction

- **Survivorship bias**: datasets that only track tokens that obtained
  liquidity / were listed study a biased subset. This engine mitigates by
  discovering tokens at t0 (radar/prelaunch) and by labeling from *persisted*
  market history, but the benchmark must still count tokens that "died on
  arrival" (no liquidity ever) separately from tokens that lived.
- **Label imbalance**: collapses/rugs vs. genuine breakouts are vastly
  imbalanced depending on definition. The engine's own `serpent.db` shows
  ~9.1% collapse among evaluated outcomes — a 10:1 imbalance. Accuracy is
  therefore meaningless; precision@K, calibration, and concordance are the
  metrics that survive imbalance.
- **Adversarial dynamics**: pump-and-dump operators adapt to detection. Unlike
  weather, the *generating process changes in response to the tool*. Bolz et
  al. (2024, arXiv:2412.18848, "Machine Learning-Based Detection of
  Pump-and-Dump Schemes in Real-Time", University of Zurich) document exactly
  this: models that work on historical shill-linguistics degrade as operators
  change tactics. Consequence for evaluation: results must be reported per time
  window (drift-aware), and a model that only worked in the past must be
  labeled "not currently demonstrated" — this is what the engine's
  `forecast_drift` health already attempts; the benchmark harness must measure
  the same thing with fixed methodology.

### 1.6 Time-to-event / survival-analysis evaluation

- The system has hazard models for time-to-peak and time-to-collapse. Standard
  classification metrics do not evaluate these correctly because of
  *right-censoring*: a token that has not collapsed by the end of the window is
  not a "negative" — it is a censored observation (it may collapse tomorrow).
- Standard practice (see scikit-survival's evaluation guide; Graf et al. 1999
  "Assessment and comparison of prognostic classification schemes for survival
  data", *Statistics in Medicine*; Uno et al. 2011 on IPCW concordance):
  - **Harrell's concordance index (C-index)**: the fraction of comparable
    pairs where the model's risk ranking agrees with the observed event order.
    This is the primary discrimination metric for time-to-event models.
  - **Time-dependent / integrated Brier score**: extends Brier to censored
    survival data; the integrated Brier score (IBS) is the calibration +
    discrimination summary over the whole horizon.
  - **Censoring must be explicit**: observations still alive at horizon end are
    right-censored, never treated as success or failure.
- **Decision adopted:** for hazard outputs, compute C-index (Harrell) with
  explicit censoring and report per-horizon event rates; do not use binary
  accuracy on "did it collapse within 24h" as the sole judge of a survival
  model.

### 1.7 Statistical significance

- Small samples are the norm for rare events (collapse/ignition labels numbered
  in the dozens on a fresh run). Standard practice:
  - **Wilson score interval** (Wilson 1927) for binomial proportions — unlike
    the Wald interval it does not collapse at p=0 or p=1 and has correct
    coverage at small n (McGrath & Zhao 2024, *The American Statistician*,
    "Binomial confidence intervals for rare events", emphasize relative margin
    of error for rare events).
  - Minimum sample thresholds before a result is *meaningful*, stated
    numerically (Stage 1.4).
- **Decision adopted:** every observed frequency in the field report carries a
  Wilson 95% CI; no metric is "green" unless its CI excludes the naive baseline
  (or the CI is too wide to distinguish — then it is `inconclusive`).

---

## 2. Mapping methodology to this system's actual outputs

Read against the actual code: `scoring/formulas.py`, `scoring/engine.py`,
`scoring/ensemble.py`, `scoring/llm_calibration.py`, `risk_engine/rules.py`,
`risk_engine/outcomes.py`, `forecast/engine.py`, `forecast/hazard.py`,
`pump_physics/engine.py`, `common/enums.py`.

### 2.1 RiskBand (GREEN/YELLOW/ORANGE/RED/BLACK) — ordinal categorical

- The band is **ordinal, not nominal**: GREEN→YELLOW is a near-miss, GREEN→BLACK
  is a catastrophic miss. Flat multiclass accuracy treats them equally, which is
  wrong. Harrell's ordinal-regression practice (Harrell, *Regression Modeling
  Strategies*, 2nd ed., 2015) is the reference for why ordered categories need
  ordered evaluation.
- **Metrics:** 
  - **Mean absolute band-distance** (rank distance 0..4, e.g. GREEN=0, BLACK=4)
    between predicted and realized band — the primary ordinal error.
  - **Ordinal agreement table** (confusion over the 5×5 lattice) so directional
    bias (systematically too lenient vs. too harsh) is visible.
  - **Per-band collapse precision** with Wilson CIs, vs. naive always-negative
    baseline.
  - This replaces flat accuracy entirely.

### 2.2 The ten `scoring/formulas.py` scores

| Score | Empirically checkable? | Why / how |
|---|---|---|
| `hype` | Yes (weak proxy) | Not directly falsifiable (there is no ground-truth "hype"), but testable as a *ranking* predictor: does rank-by-hype predict forward price peak (ignition)? Check concordance vs. ignition labels + precision@K. |
| `ethos` | Weak proxy | No ground-truth "legitimacy". Test as ranking predictor of survival only. |
| `risk` | **Yes** | This is the direct collapse predictor (feeds RiskBand). Fully checkable vs. collapse outcomes; concordance, calibration, precision@K. |
| `liquidity_access` | Yes (proxy) | Checkable as ranking predictor of survival/liquidity persistence. |
| `manipulation` | Weak proxy | No ground truth for manipulation intent; test only that it correlates with collapse (concordance), never as an absolute. |
| `confidence` | **Yes — calibration** | A confidence score is a probability-like statement; the correct test is calibration: among scores with confidence≈c, is the "positive outcome" rate ≈c? Also check that high-confidence scores rank better (resolution). |
| `uncertainty` | **Yes — inverse calibration** | Should be *anti*-correlated with outcome accuracy; check that high-uncertainty predictions are worse-calibrated. |
| `catalyst` | Yes (weak) | No ground-truth catalyst calendar in the benchmark scope; test only as ranking predictor of ignition when catalyst data exists. |
| `exit_risk` | **Yes — survival-style** | Directly checkable vs. time-to-collapse (concordance on censored data). |
| `research_priority` | **Non-benchmarkable — flagged** | This is a *work-prioritization* heuristic (which token should an analyst look at first), not an empirical claim about the market. There is no falsifiable ground truth for "should have been researched first" without an action-feedback loop that does not exist in the data. **Explicitly excluded from empirical benchmarking**; documented, not forced through a metric. |

### 2.3 `collapse_probability_24h` and other calibrated probabilities

- **Metrics:** Brier score, ECE (10 bins), reliability histogram, rank-AUC, and
  precision@K, all with Wilson CIs, all compared to the naive base-rate
  predictor (`Brier = p(1-p)`, `precision@K ≈ base rate`).
- The `calibration_bucket` field on `Forecast` rows makes the reliability
  histogram directly computable from persisted data.

### 2.4 Hazard model time-to-peak / time-to-collapse

- **Metrics:** Harrell C-index with right-censoring (observations past the
  forward horizon are censored), per-horizon event-rate curves, and a
  check that `expected_hours_to_collapse` is monotone in actual survival time.
  No binary "did it collapse" accuracy as the headline for these outputs.

### 2.5 Ensemble weight adaptation — the meta-metric

- The question is not "is the ensemble good" but "does the adaptive weighting
  track which scorer is currently accurate". Design a **weight-tracking test**:
  - For a synthetic period where the rule scorer is perfect and ML is noise,
    the learned rule weight must rise toward the ceiling and ML's fall;
  - then flip the roles and verify the weights *follow* (within N recalibration
    cycles).
- On real data: measure the correlation between each scorer's *trailing window
  accuracy* (computed independently by the harness) and the *assigned weight*
  trajectory persisted in `EnsembleState.weight_history`. If the weights do not
  track trailing accuracy, the adaptation is drifting arbitrarily and the
  ensemble's blended output must not be credited.

---

## 3. Ground truth definitions

These definitions are the contract for every label the benchmark uses. They are
deliberately aligned with the engine's own lifecycle thresholds so the harness
evaluates the system against the system's declared semantics.

### 3.1 Label categories (precise, falsifiable)

- **RUG**: a token whose lifecycle state machine reaches `RUGGED` — i.e.
  `withdrawal_events >= 1` AND `liquidity_usd <= 0` (pump_physics/engine.py).
  Ground truth: `lifecycle_events.phase == 'rugged'`.
- **COLLAPSE**: either (a) lifecycle phase `collapse` (`one_hour_return <=
  -25%` or a withdrawal event), (b) the forecast label `collapse` (trough
  price ≤ -70% of entry within 24h — `forecast_collapse_threshold = -0.70`),
  or (c) `risk_outcomes.collapsed == 1` (evaluated as lifecycle phase in
  collapse/dead/rugged). The benchmark uses (b) as the primary definition
  (it is numeric and label-engineered), and (c) where only outcome rows exist.
- **GENUINE BREAKOUT / IGNITION**: lifecycle `ignition` or the forecast label
  `ignition` (peak price ≥ +20% of entry within 24h —
  `forecast_ignition_threshold = 0.20`).
- **DEAD ON ARRIVAL**: lifecycle `dead` (no trades for ≥ 168h) or an asset that
  never obtained a tradable pool / never had ≥1 price observation within the
  first 24h of tracking. These are counted separately from collapses — they are
  not "negatives", they are a distinct category.
- **SURVIVOR**: lifecycle `survivor`/`parabolic`/`saturation` at eval time with
  no collapse.

### 3.2 Partial / ambiguous cases

- A token that stalls — never peaks ≥ +20% and never troughs ≤ -70% — is
  labeled `STALL`, a distinct bucket. STALL is *not* treated as "no collapse"
  for collapse metrics without stating so: collapse metrics use only
  collapse/non-collapse with STALL counted as non-collapse **and** reported
  separately so the reader sees the ambiguity.
- Tokens whose outcome is unobservable within the window (still alive at
  horizon end, price history truncated) are **right-censored** for survival
  metrics and **excluded** from binary metrics (not silently treated as
  negative).

### 3.3 Minimum data requirements for inclusion

- A token enters the benchmark only if it has at least **24h** of market
  snapshot history *after* its decision time (matching the forward horizon),
  or reaches a terminal lifecycle phase (rugged/dead) inside that window.
- Tokens with fewer than 3 price observations in the forward window are
  excluded and counted as `insufficient_history`, never as negatives.
- Reported sample sizes are always explicit.

---

## 4. Benchmark success criteria (stated numerically)

### 4.1 Naive baselines (mandatory comparison points)

Every model metric is reported **side by side** with at least these baselines:

1. **Base-rate predictor**: predicts the observed collapse/ignition rate `b`
   everywhere. Brier baseline = `b(1-b)`; precision@K baseline ≈ `b`.
2. **Random ranking**: expected precision@K = `b` (Wilson CI computed on the
   actual random draw); rank-AUC = 0.5.
3. **Single-feature heuristic**: "liquidity depth only" — rank tokens by
   `liquidity_depth` and take its precision@K / concordance. A mediocre model
   must still beat this.

### 4.2 Minimum sample sizes and significance

- **Per metric cell** (per output × per regime × per band where relevant):
  - n ≥ 30 to report a frequency at all (a Wilson CI on n=30, p=0.1 spans
    ~[0.02, 0.27] — wide but usable).
  - n ≥ 100 to treat a result as *demonstrated* (CI half-width ≤ ~0.06 at
    p=0.1).
  - n < 30 ⇒ `insufficient_data`, reported as such, never averaged away.
- **Verdict rules** (95% Wilson CI):
  - `better_than_baseline`: model metric CI lies entirely above the baseline
    point estimate AND above the baseline CI's lower bound.
  - `indistinguishable_from_baseline`: model CI overlaps the baseline CI.
  - `worse_than_baseline`: model CI entirely below baseline.
  - `insufficient_data`: too few samples to reach any of the above.

### 4.3 What "worth trusting" means

- **Trust a probability output** when: Brier < `b(1-b)` and ECE ≤ 0.10 (the
  engine's own drift ceiling for calibration error) with n ≥ 100, and the
  reliability histogram shows no bin with |predicted − observed| > 0.15.
- **Trust a ranking output** (risk band, exit risk, precision@K flags) when:
  concordance CI excludes 0.5, precision@K CI excludes the base rate, and the
  ordinal band-distance is below the always-negative classifier's distance,
  each with n ≥ 100.
- **Anything suspiciously good** (metrics that exceed what a *non-leaked*
  feature set could plausibly produce, e.g. near-perfect concordance on
  engineered features) is cross-checked against the leakage-detection self-test
  pattern (Stage 2.2 case 3) before being reported as genuine.

---

## 5. Deliverable for this stage

This document. It is the fixed contract for Stages 2–4: the harness design
(Stage 2) implements exactly these metrics, the synthetic self-tests (Stage 2.2,
built in Stage 3) prove the harness measures them correctly, and the field
report (Stage 4) reports them per this document's verdict rules.

No benchmark code has been written yet, per the Stage 1 hard gate.
