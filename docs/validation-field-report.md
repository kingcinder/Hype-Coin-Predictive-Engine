# Phase 9 — Validation Field Report

**Deliverable:** Stage 4 of the Phase 9 validation/benchmark harness.
**Harness:** `validation/` package (self-tested against three synthetic known-answer cases; 12 regression tests).
**Source data:** `reports/validation-2026-08-25T234141.908999_0000.json` (report format v1, git `9288728`).
**Generated:** 2026-08-25T23:41:41 UTC · model `engine-current` · database `serpent.db`.

This report states what the benchmark found, without softening. Where the data says an output is
not better than noise, that is what is written here.

---

## 1. Executive summary

The harness was run against the real engine's accumulated data. The honest summary is short:

| Category | Verdict |
|---|---|
| RiskBand ordinal distance | **Indistinguishable from the laziest baseline** (always-GREEN) |
| RiskBand BLACK collapse precision | **Indistinguishable from base rate** (9.09% vs 9.09%) |
| `risk_score` concordance | **Suspicious-good, not genuine** — degenerate 2-value distribution, flagged by leakage cross-check |
| `exit_risk` concordance | **Suspicious-good, not genuine** — same degenerate cause |
| `uncertainty` error-correlation | **Suspicious-good, not genuine** — same degenerate cause |
| Confidence calibration | **Worse than baseline** (0.32 vs 0.10 trust ceiling) |
| `collapse_probability_24h` (4 metrics) | **No verdict possible** — 0 forecasts ever persisted |
| Hazard time-to-collapse C-index | **No verdict possible** — 0 matched pairs |
| Ensemble weight adaptation (rule/ML/heuristic) | **No verdict possible** — 1/0/0 weeks of weight history |

**The engine's scoring layer currently demonstrates no predictive value that is distinguishable
from noise — and the one family of numbers that looks spectacular (concordance 1.0) is a
statistical artifact, not evidence of skill.** The probabilistic layer (forecasts, hazard, ensemble
adaptation) has never produced enough data to be evaluated at all.

The benchmark answers the phase question directly:

- **Is this data worth collecting?** Yes, the outcome-labelling pipeline is producing labels
  (1,574 evaluated outcomes), but the *scoring* outputs are not yet worth acting on.
- **Is this model worth trusting?** No. Not one output is demonstrably better than its naive
  baseline, and the three suspicious-good results must be treated as artifacts pending the fixes
  in §5.

---

## 2. What was evaluated

Per the Stage 1 methodology (§1.2 mapping), each engine output was mapped to a metric and a
naive baseline, with minimum-sample gates:

| Engine output | Metric | Baseline | Benchmarkable? |
|---|---|---|---|
| RiskBand (GREEN→BLACK) | Ordinal band distance | Always-GREEN predictor | Yes |
| RiskBand:BLACK | Collapse precision@BLACK | Base collapse rate | Yes |
| `risk_score` | Concordance | Random ranking (0.5) | Yes — *flagged* |
| `exit_risk` | Concordance | Random ranking (0.5) | Yes — *flagged* |
| `confidence` | Calibration error (ECE-style) | Trust ceiling 0.10 | Yes |
| `uncertainty` | Error-correlation | 0 (no tracking) | Yes — *flagged* |
| `hype`, `ethos`, `liquidity_access`, `manipulation`, `catalyst`, `research_priority` | — | — | **No** — no ground truth exists for these (see §6) |
| `collapse_probability_24h` | Brier / ECE / precision@10 / concordance | Base rate / 0.5 | Yes — insufficient data |
| Hazard time-to-peak / time-to-collapse | Harrell C-index (right-censored) | 0.5 | Yes — insufficient data |
| Ensemble weights | Weight↔accuracy correlation per scorer | 0 | Yes — insufficient data |

---

## 3. Data reality check (Stage 1.3/1.4 gates)

Before any number is meaningful, the sample-size gates from the methodology must be checked:

| Database state | Count | Gate |
|---|---|---|
| Assets | 290 | — |
| Scores (formula outputs) | 2,824 | — |
| RiskOutcome rows | 2,429 | — |
| **Evaluated outcomes** | **1,574** | min 30/cell — passes for cell-level metrics |
| Forecasts (`collapse_probability_24h`) | **0** | min 30 — **fails, nothing to evaluate** |
| Label rows | 8 | min 30 — **fails** |
| Lifecycle events | 228 | — |
| Regime stratification | **1 regime** ("unclassified") | ≥2 regimes required for regime-stratified results — **fails** |

**The regime-stratified comparison promised by the methodology is impossible on this data.**
The entire historical window collapses into a single regime, so every result below is reported
as `unclassified`. This is itself a limitation, not a design shortcut: the harness partitions
correctly, but the available window contains no detectable regime boundary.

**The forecast/hazard/ensemble layers cannot be benchmarked.** The engine has persisted **zero**
Forecast rows. The ML calibration layer was never trained (8 labels is far below any usable
minimum), so no probability forecasts were ever written to the database. The harness correctly
reports `insufficient_data` rather than inventing numbers — no verdict is possible on an output
that never ran in production.

---

## 4. Results by output category

All intervals are 95% bootstrap CIs. "Baseline" is the naive comparison from §2. Every number
is from the versioned report; sample sizes sit next to every figure.

### 4.1 RiskBand — ordinal band distance

| | Model | Baseline (always-GREEN) |
|---|---|---|
| Ordinal distance | **2.00** | 2.00 |
| 95% CI | (1.80, 2.22) | (1.78, 2.20) |
| n | 286 (known outcomes) | 286 |
| Verdict | **indistinguishable_from_baseline** | |

1,287 ambiguous outcomes were excluded from this metric per methodology §3.2 (never treated as
positives *or* negatives).

The engine's band assignments carry **no ordinal information beyond what the laziest possible
classifier achieves**. A GREEN-always predictor and the engine's band assignments are
statistically indistinguishable at n=286.

### 4.2 RiskBand:BLACK — collapse precision

| | Model | Baseline (base rate) |
|---|---|---|
| Collapse precision in BLACK | **9.09%** | 9.09% |
| 95% CI | (7.77%, 10.61%) | (7.77%, 10.61%) |
| n | 1,573 | 1,573 |
| Verdict | **indistinguishable_from_baseline** | |

The BLACK band — the engine's strongest "this will collapse" signal — flags tokens at exactly the
base collapse rate. **The band's most extreme output contains zero information about collapse.**
(1,287 ambiguous outcomes are reported as non-collapse here, per the report note; the ordinal
metric in 4.1 excludes them entirely. Both treatments are consistent with the methodology and
neither changes the verdict.)

### 4.3 `risk_score` and `exit_risk` — concordance: SUSPICIOUS-GOOD

| | risk_score | exit_risk |
|---|---|---|
| Concordance | **1.0000** | **1.0000** |
| 95% CI | (1.0, 1.0) | (1.0, 1.0) |
| Baseline | 0.5 | 0.5 |
| n | 286 | 286 |
| Verdict | better_than_baseline | better_than_baseline |
| **Leakage cross-check** | **SUSPICIOUS — flagged** | **SUSPICIOUS — flagged** |

**This is not genuine predictive power, and the field report does not claim it is.** Root-cause
investigation against the database:

- In the evaluated-outcome subset (n=286), `risk` takes **exactly two values**: 66.66 for all
  143 collapsed tokens in the subset (144 across the full table), 59.99 for all 143 survived
  tokens. A two-point distribution *trivially* separates two outcome classes — concordance 1.0
  is a mathematical consequence of having two distinct score values, not of ranking skill.
- The same holds for `exit_risk` (45.78 vs 36.0) and `uncertainty` (§4.5).
- **The separation does not survive outside the evaluated subset.** Across all 2,429 outcome
  rows, `risk` = 66.66 appears on 26 *unclassified* tokens (neither collapsed nor survived) and
  `risk` = 59.99 covers 1,853 unclassified tokens. The "perfect" score-to-outcome alignment is
  a property of the small evaluated slice, not of the score.

The engine's scoring formulas are producing **degenerate, nearly-constant outputs** (scores
rounded to values like 59.99/66.66 — visibly saturation-capped or threshold-quantized), and the
harness's point-in-time leakage detector correctly refused to certify them. **Treat any
concordance ≥0.95 in this system as an artifact until the score range is fixed.**

### 4.4 `confidence` — calibration error

| | Model | Baseline (trust ceiling) |
|---|---|---|
| Calibration error | **0.32** | 0.10 |
| 95% CI | (0.31, 0.34) | (0.10, 0.10) |
| n | 1,573 | — |
| Verdict | **worse_than_baseline** | |

The engine's reported confidence **does not track observed survival frequency** — it misses the
trust ceiling by 3.2×. This is the one cleanly-measurable calibration result in the run, and it
is a failure.

### 4.5 `uncertainty` — error-correlation: SUSPICIOUS-GOOD

| | Model | Baseline |
|---|---|---|
| Error-correlation | **1.0000** | 0.0 |
| 95% CI | (1.0, 1.0) | (−0.15, 0.15) |
| n | 1,573 | — |
| Verdict | better_than_baseline | |
| **Leakage cross-check** | **SUSPICIOUS — flagged** | |

Directionally this is the "desired" sign (uncertainty tracks error), but the magnitude is
untrustworthy for the same reason as 4.3: in the known-outcome evaluated rows (n=286)
`uncertainty` takes two values (45.11 for collapsed, 100.0 for survived, one collapsed outlier at
49.96) that perfectly separate the classes. Flagged, not certified.

### 4.6 Probability forecasts, hazard, ensemble adaptation — insufficient data

| Output | Metric | n | Verdict |
|---|---|---|---|
| `collapse_probability_24h` | Brier | 0 | insufficient_data — no Forecast rows |
| `collapse_probability_24h` | ECE | 0 | insufficient_data |
| `collapse_probability_24h` | precision@10 | 0 | insufficient_data |
| `collapse_probability_24h` | concordance | 0 | insufficient_data |
| Hazard time-to-collapse | C-index | 0 | insufficient_data — 0 matched pairs |
| Ensemble weight: rule | weight↔acc corr | 1 | insufficient_data |
| Ensemble weight: ML | weight↔acc corr | 0 | insufficient_data |
| Ensemble weight: heuristic | weight↔acc corr | 0 | insufficient_data |

No verdicts are offered for these outputs. The harness emits `insufficient_data` — it does not
fabricate a number to fill the table.

---

## 5. What the suspicious-good cross-check proved

The Stage 2/2.2 leakage-detection self-test (injected feature perfectly correlated with the
future outcome → harness must flag it) was validated on synthetic data. Applied to the real run,
it flagged **3 of 14 cells** (risk_score, exit_risk, uncertainty — all concordance/error-
correlation ≥ 0.95). The cross-check did its job: it prevented a degenerate score distribution
from being reported as a spectacular result.

Root cause (verified against the database, not assumed):

1. **Scores are quantized to near-constant values.** `risk` ∈ {59.99, 66.66} and `exit_risk`
   ∈ {36.0, 45.78} across all known-outcome evaluated rows (n=286) — the formula layer is
   emitting saturated/threshold-quantized outputs with almost no within-class variance.
2. **The two values happen to align with the two outcome classes in the evaluated slice.**
3. **The alignment is an artifact of the slice, not the score** — it does not generalize to the
   full outcome table.

The benchmark therefore *refutes* — not confirms — the apparent skill. Any future run that
reports concordance ≥ 0.95 must show evidence the score distribution is not two-valued before it
can be taken seriously.

---

## 6. Non-benchmarkable outputs (per methodology §1.2)

- **`hype`, `ethos`, `liquidity_access`, `manipulation`, `catalyst`, `research_priority`** — no
  ground truth exists or can exist for these (no objective, time-stamped "true hype" measurement).
  They are design inputs, not falsifiable predictions. The methodology flags them as
  **non-benchmarkable**, and this report does not force a metric onto them.
- **`research_priority`** specifically: it is definitionally unfalsifiable (a priority ranking
  has no independent ground truth).

These are excluded from the verdict table by design, not by accident.

---

## 7. Known limitations

1. **Single regime.** No regime boundary was detectable in the historical window; every result is
   `unclassified`. Bull/bear/dead-period stratification (methodology §1.1) is untested.
2. **Small known-outcome sample.** Only 286 of 1,573 in-window evaluated rows have a
   definitive collapsed/survived label; 1,287 are ambiguous (the database digest counts 1,574
   evaluated outcomes total, 1 of which falls in the reference window). Power to detect small
   real effects is low.
3. **Forecast layer never ran.** 0 Forecast rows, 8 labels. The probability outputs — the system's
   most distinctive claimed capability — are entirely unevaluated.
4. **Walk-forward reference set nearly empty** (1 of 1,574 rows in the reference window), so the
   embargo/purge controls are untested on real data.
5. **Suspicious-good artifacts** (§5) dominate the two "better than baseline" cells; neither is
   reportable as evidence.

---

## 8. Recommendations

**Worth trusting / acting on today: nothing in the scoring layer.** No output is demonstrably
better than its naive baseline, and the apparent winners are artifacts.

**Needs more data before a verdict is possible:**
- `collapse_probability_24h` and hazard C-index — enable the label bootstrap to accumulate
  ≥30 forecast/outcome pairs, then rerun. This is the single most important follow-up: the
  system's flagship output has never been measured.
- Ensemble weight adaptation — needs ≥3–5 weeks of persisted weight + outcome history.
- Regime-stratified results — needs a window with a detectable regime boundary.

**Shows no evidence of predictive value at all:**
- RiskBand ordinal distance (indistinguishable from always-GREEN).
- RiskBand:BLACK collapse precision (identical to base rate).
- `confidence` calibration (worse than the 0.10 ceiling by 3.2×).

**Immediate engineering action required:**
1. **Fix the degenerate score distribution.** `risk` and `exit_risk` emitting two values across
   the whole corpus is a formula bug (saturation/threshold quantization), not a tuning issue.
   No ranking metric is meaningful until scores have real variance.
2. **Do not report the 1.0 concordances as validation results.** They are artifacts; the harness
   flagged them for exactly this reason.
3. **Decide whether RiskBand carries information at all.** At n=286 it is indistinguishable from
   a constant classifier; either the band thresholds need work or the band output should be
   demoted from a "prediction" to a heuristic label.

---

*This report was produced by the Phase 9 harness (self-tested on synthetic known-answer data
including a deliberately injected leakage pattern). The methodology is documented in
`docs/validation-methodology.md`; the harness design and pre-committed self-test expectations in
`docs/validation-harness-design.md`; the raw versioned data in `reports/`.*
