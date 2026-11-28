# Mission Refactor — Design Spec (2026-11-28)

Status: design spec. This document argues from a completed research + audit
round (2026-11-28) over the merged tree (main at `f5c2079`). It changes no
code by itself; it is the spec the implementation plan argues from.

## 1. Mission (new statement)

The engine exists to do two things, in order:

1. **Entry — catch new moons.** Identify coins that are about to make a
   sharp rise in valuation *before* the rise, while they are still new
   moons: newly launched, thinly held, not yet repriced by the crowd.
2. **Exit — reap the harvest moon.** For coins the engine is in (or
   tracking), find the highest point the position can *reliably* be
   exited at — near the peak, before the impending crash, while liquidity
   is still solvent enough to exit a potentially large position.

The engine is **not** a safety or stability finder. It does not look for
coins that will last the test of time, and "this coin is unlikely to
collapse" is not a success criterion. A coin that pumps hard and then
collapses is a **success case** if the entry was early and the exit was
near the peak. Collapse prediction survives in the new mission only as an
exit-side input: it helps time the harvest, it does not veto the trade.

This mission is not new text. `docs/blueprint.md` already states it — its
goal line is to predict "which emergent tokens are about to *ignite*
(spike hard right after launch/ICO) and which ignited tokens are about to
*collapse*," and its thesis is that hype leads price, liquidity follows,
and the engine's job is to predict phase transitions, not price. What
drifted is the rest of the repo: labels, gates, ranking formulas, and
validation grew up around the collapse axis until the engine was, in
practice, graded as a safety filter. This refactor promotes the blueprint
mission to the whole repo — mission statement, labels, gates, ranking,
and UI language all read the same way.

Two consequences follow immediately and govern everything below:

- Fitness is measured on **pump prediction** (forward max-gain hit rate,
  lead time) and **exit timing** (executable value captured vs the
  executable peak), never on collapse-avoidance or band "safety."
- The lifecycle machine `SEEDING → IGNITION → PARABOLIC → SATURATION →
  COLLAPSE` (plus `DEAD`/`RUGGED`/`SURVIVOR`) in `pump_physics/engine.py`
  is the shared spine of both algorithms: the entry algorithm works the
  first transitions, the exit algorithm works the last ones, and both
  read the same phase state.

## 2. Design principles

1. **Two algorithms, one spine.** Entry ("New Moon") and exit ("Harvest")
   are separate models with separate labels, evaluators, and gates,
   sharing ingestion, features, and the lifecycle state machine. Neither
   borrows the other's success metric.
2. **Executable value, not spot price.** Anything exit-side is scored in
   proceeds a real position could actually extract from the pool, at a
   stated position size. Spot-price peaks are fiction for a large exit.
3. **Point-in-time or it didn't happen.** Training and evaluation use
   data as it was observed at the decision timestamp — as-observed
   snapshots, first-observation flags, point-in-time wallet scores.
   Restated vendor data, full-history aggregates, and any feature whose
   value was not knowable at decision time are lookahead and are treated
   as defects, not noise.
4. **Prevalence discipline.** Moonshots are ~1%-prevalence events. At
   that base rate, a classifier needs a false-positive rate of ~1–2% to
   produce usable precision. Accuracy is a banned metric on the entry
   side; the reportable numbers are precision@k per day and PR-AUC.
5. **Every component ships with its falsification test.** Each signal,
   layer, and model in §§3–4 names the test that kills it and the bar it
   must clear. A component that cannot state its kill test is not built.
6. **Replay before live.** The point-in-time replay harness (§3, item 0)
   is the shared falsification instrument. No signal is promoted on
   backtest-by-inspection, and no gate is declared passed on the window
   the signal was screened on.
7. **Screening, weights, evaluation — three disjoint windows.**
   Time-split windows A/B/C are used for feature screening, weight
   fitting, and evaluation respectively, with Benjamini–Hochberg
   correction across screened features. A result quoted from the
   screening window is a hypothesis, not a result.
8. **The data lake is the asset; models are replaceable.** Raw evidence
   is preserved verbatim and forever (§7). Labels and models are derived
   artifacts and may be regenerated, replaced, or discarded without
   ceremony.
9. **Staged, never single-shot, on exits.** Verified over 802 tested
   blow-off climaxes: 71.3% made a new high before closing below the
   climax low (TradersJournal). An exit design that fires the whole
   position on one climax signal forfeits that second high by
   construction; exits are ladders paced by signals (§4).
10. **GREEN means one thing.** Under the new mission GREEN can only mean
    "no exit signal yet." Any surface — band, UI copy, report — that
    reads GREEN as "safe" is a defect (§8).

## 3. Entry algorithm — "New Moon"

The entry problem: rank newly observed coins so that the top of the list
is enriched for coins about to make a sharp forward move, early enough
that a position can still be established near the pre-rise price. The
ranking target is the continuous forward label set of §5 — primarily
`MFE_6h` (maximum forward gain within 6h) with the binary head
`y = 1{MFE_6h ≥ 1.0}`.

### Item 0 — the replay harness comes first

Before any signal work: a point-in-time **replay harness** that walks
snapshot history in timestamp order, exposes only what was observable at
each step, and simulates entry at the *next* snapshot after a signal
fires, with price impact applied. This is the instrument every item
below is falsified on. Entry-at-signal-price backtests are banned: they
assume execution at a price that existed only before the signal could
have been computed.

### Item 1 — Net-inflow + curve-velocity rank composite

Rank coins by net quote inflow and its velocity, computed from pool
reserves: net inflow = Δ(quote reserves) / reserves over the observation
interval. **Not volume.** Verified mechanism (Streamflow): bonding-curve
graduation depends on net SOL in the curve; wash volume round-trips
while net reserves barely move, so volume ranks the bots and net inflow
ranks the demand. The composite is a rank-sum within the contemporaneous
cohort — the same machinery already proven on the collapse side
(`scripts/forward_validate_ranksum.py`, fixed threshold 0.7546 from
`reports/ranksum-reference-20261121.json`) — re-aimed at forward gain.

*Falsification:* 14-day replay. precision@top-20/day for
`MFE_6h ≥ 2x` must beat a plain volume-ranking baseline by ≥ 1.5×, or
the composite is killed and volume's failure is documented instead.

### Item 2 — Wash-adjusted unique-buyer acceleration

`unique_buyers_estimate` is already a stored feature; the signal is its
**second difference** (acceleration), cleaned by excluding
same-transaction round trips. The cleaning is not optional: 58.4% of
Solana DEX volume has been flagged as not real (Bitquery), and 80.7% of
all flagged volume across six chains is same-transaction round trips
(Bitquery). Raw buyer counts measure wash infrastructure; acceleration
of *cleaned* unique buyers measures arriving humans.

*Falsification:* same replay as item 1, ablation — composite with and
without this term; the term stays only if precision@top-20/day improves
on window C, not on the screening window.

### Item 3 — Social-presence prior

Telegram / X / website presence flags **as observed at first
observation of the coin**, applied as a prior multiplier on the rank,
never as a standalone signal. Verified magnitudes (arXiv 2607.02823v3,
832,941 launches): pooled graduation rate 0.198% (the paper's own caveat:
a fast-regime lower bound), Telegram presence 1.485% vs 0.166% without —
an 8.94× lift; median time-to-graduation 1.66 minutes. The prior is
gameable (presence is cheap to fake), so its weight is modest and
re-estimated per regime; a prior estimated in one regime is not carried
into another unexamined.

*Falsification:* lift of the prior on window C vs window A estimate; if
the window-C lift falls below half the window-A estimate, the prior is
re-estimated or dropped, not defended.

### Item 4 — Bundle + cluster insider gate

Measure the share of early supply taken by same-block bundles and, as
v2, by funding-graph clusters (wallets sharing a funding source). This
is a **gate**, not a rank term: insider-controlled float changes the
character of the trade rather than its expected value. Rule: a single
cluster above 40% of early float means the coin is entered only with
exit monitoring armed from the first block, or not entered at all.

*Falsification:* case-control on replay — do gated-out coins show
materially worse captured-fraction outcomes (§4) than passed coins at
the same rank? If the gate removes as many good trades as bad ones at
equal rates, it is decoration and is removed.

### Item 5 — Smart-money convergence

A point-in-time wallet-PnL table (each wallet's realized PnL computed
only from data before the decision timestamp — full-history PnL is
leakage) plus a convergence count: how many historically profitable
wallets entered this coin in the trailing window. Highest ceiling of
the five items, highest effort; built last and gated behind its test.

*Falsification:* case-control — coins with ≥ 3 profitable-wallet entries
in the 30 minutes pre-move vs matched controls; the convergence share
among pumps must be ≥ 3× the share among controls, or the table is not
built out further.

### What already exists and carries over

`forecast/engine.py` already runs dual HistGBM heads (ignition +
collapse, isotonic-calibrated) and persists `p_ignition_24h` and
`expected_hours_to_peak`. The ignition head is the seed of the New Moon
ranker, not a competitor to it: items 1–5 are the feature programme that
feeds it, and the rank composite is the baseline it must beat. Known
defect to fix in passing: `forecast/engine.py` (~L700) writes
`metrics[f"hazard.{slug}.mean_hours_to_peak"] = fit.mean_time_to_collapse`
— the peak-hazard metric is populated from the collapse fit.

**Freshness is part of the seed's contract.** Entry evaluation (§6)
reads persisted forecasts, so it first checks the newest
`forecasts.decision_ts`: if that is stale beyond a configured threshold
(`forecast_max_staleness_hours`), `entry_validated` reads **False**
with reason "stale forecasts" instead of evaluating stale rows as if
they were current signal. A silent forecast pipeline must fail the
gate loudly, never pass it on old numbers (see §10).

## 4. Exit algorithm — "Harvest"

The exit problem is defined over a held position of size `Q`, not over
price. The core object is **executable value**:

```
EV(t, Q) = proceeds from selling Q into the pool at time t,
           via the AMM curve (constant-product: Y·Q / (X + Q))
```

All exit scoring, labels, and metrics are in proceeds at a stated `Q`.
**Peak Executable Value** `PEV = max_t EV(t, Q)`. The headline metric
for any exit policy is **Captured Fraction** `CF = V(policy) / PEV`,
with regret decomposed into three additive causes: *timing* (sold
before/after the window), *impact* (tranches too large for the depth),
and *early* (exited before the position's episode was over). Benchmarks
every candidate policy must be replayed against: fixed-horizon sell,
fixed gain ladder, clairvoyant-price-peak sell (an upper bound that
itself overstates what was extractable — it ignores impact), and
hold-to-ruin (the floor). A candidate ships only if it beats the dumb
fixed ladder on **both median and p25 CF** on held-out episodes.

### Layered architecture

Each layer is independently falsifiable; layers arm, accelerate, pace,
complete, or override — none silently replaces the layers under it.

1. **Floor — fixed gain ladder.** A static tranche schedule on forward
   gain (e.g. sell a tranche at each multiple of entry EV). This is the
   benchmark every smarter layer must beat, and the behaviour the system
   falls back to when signals are absent.
2. **Arming — flow exhaustion.** Two signals arm the ladder (raise its
   pace / pull tranches earlier): **S1** net-flow rollover (net quote
   inflow changes sign or rolls over its trailing peak) and **S2**
   buyer-growth stall (cleaned unique-buyer growth flattens while price
   still rises). Arming never sells by itself.
3. **Acceleration — distribution forensics.** Three signals accelerate
   an armed ladder: depth-normalized effort-vs-result (rising volume
   against shrinking price response), top-holder net flow turning
   negative, and holder-count stall combined with rising concentration.
   These are the traces of distribution into strength.
4. **Pacing — liquidity decay.** Tranche size is capped at 10–15% of
   *current* quote depth, recomputed per tranche. As depth decays, the
   ladder slows its per-tranche size rather than accepting worse impact
   — this is the layer that keeps a large position exitable at all.
5. **Completion — structural confirmation.** Failed retest / lower-high
   structure, or the collapse-stack derivative (the existing collapse
   model and `exit_risk` re-aimed per §8), completes the exit: remaining
   tranches execute on the confirmation rather than waiting for the
   ladder.
6. **Runner — EV trailing.** Any residual position trails the running
   maximum of EV: a tranche exits when EV falls `k%` below its running
   max. The trail is on executable value, not spot, so impact is
   already inside the trigger.
7. **Override — hard signals.** LP removal or unlock events are an
   immediate full exit, bypassing every layer. These are the events
   after which "exit near the peak" stops being available at any size.

Staging is load-bearing, per §2 principle 9: 71.3% of blow-off climaxes
made a new high before closing below the climax low (802 tested,
TradersJournal). A single-shot climax exit is therefore a known-bad
design; climax signals arm and accelerate the ladder, they do not fire
the position.

### Build order (each step falsifies the next one's premise)

1. **Depth-lead study.** Does quote depth peak *before* the EV peak?
   Report the median lead across episodes. This is the cheapest test in
   the programme and gates the pacing layer's premise: if median lead
   ≤ 0, depth is a mirror of the peak, not a signal for it, and pacing
   is redesigned around flow instead.
2. **EV-peak trailing exit replay.** The runner (layer 6) alone against
   the §4 benchmarks on replayed episodes.
3. **Flow-exhaustion armed ladder event study.** Layers 1+2 (+6)
   against benchmarks; the arming signals must add CF over the floor
   alone or be dropped.
4. **Distribution ensemble.** Layers 3–5 added. **Data dependency:**
   this needs holder *balance deltas*, not holder counts — counts alone
   cannot show top-holder net flow. Verify the lake stores balance
   deltas before scheduling this step (§10).
5. **Harvest-window state classifier — last.** A learned classifier
   over the §5 snapshot classes (pre-harvest / in-window / post-harvest)
   carries the highest self-deception risk in the programme. It is
   trained and evaluated on **episode-level splits only** (snapshots of
   one episode never straddle train/test), and it ships only if it
   beats rule designs 2+3 combined on median and p25 CF; otherwise the
   rules ship and the classifier is documented as a negative result.

The seed of the second stage already exists: `forecast/engine.py`
persists phase-conditioned peak hazards (`peak_hazard`,
`peak_hazards_by_phase`). Under the refactor these become the
cohort-conditional second-stage exit model — hazard of the harvest
window closing, conditioned on lifecycle phase — rather than a
standalone score.

## 5. Labels

Labels are where the old mission is physically stored, so this section
is the refactor's centre of mass. Current state (audit): `forecast/labels.py`
and `data_lake/labels.py` compute `peak_pct` and `trough_pct` over the
forward window, then **discard the magnitudes** and store only binarized
`LABEL_IGNITION` (`peak_pct ≥ 0.20`, `forecast_ignition_threshold = 0.20`)
and `LABEL_COLLAPSE` (drawdown ≤ −0.70) rows. Hours-to-peak is never
computed. Everything below follows from stopping that discard.

### Entry labels (new, continuous)

- `MFE_H` — maximum forward gain within horizon `H ∈ {1h, 6h, 24h}`,
  computed from market snapshots, not from the retention-bounded
  features table. **Coverage caveat (measured 2026-11-28):** snapshots
  are not yet the deep history this label programme assumes — on the
  live DB, `market_snapshots` held 26,562 rows over 7,849 assets
  spanning 5 days (~3.4 points per asset), and 99.1% of forecasted
  assets were unevaluable for forward gains at that density. The
  density rule (§5, censoring symmetry) needs ≥ ⌈24h / 6h⌉ = 4
  snapshots in a 24h window including one in the final quartile; at
  ~3.4 points per asset over 5 *days*, almost no window qualifies.
  Generating these labels at scale is therefore **gated on the
  snapshot-coverage precondition** (implementation plan, Task 0):
  forecasted/scored assets must receive snapshots on the scan cadence
  before the label programme can run. The Parquet archive (§7) remains
  the deep raw-evidence layer; snapshots become a deep history only
  once coverage is fixed and time accrues.
- Binary head: `y = 1{MFE_6h ≥ 1.0}` (a doubling within 6h is the
  canonical "sharp rise" for the primary gate; other thresholds are
  reported, not gated).
- Regression target: `log1p(MFE)` at each horizon.
- Time-to-pump target for a survival head: time from decision timestamp
  to first crossing of the gain threshold.

**Competing risks, stated explicitly.** A coin can die before it can
pump. Death before the threshold is *not* ordinary censoring: it is a
competing event — label 0 for the binary head, and a competing-risk
event (not a non-event) for the survival head. Window end with neither
pump nor death is right-censoring and is valid **only** for the survival
head; a right-censored row must never be written as a binary 0.

**Censoring symmetry (defect fix).** `forward_evidence_sufficient`
(Ticket 1 step 5) currently gates only `collapse = 0` — a collapse 0 is
written only when the forward window has enough snapshots to have seen
a collapse. The same rule must gate gain labels symmetrically: an
`MFE`-derived 0 on a thinly observed window is an unobserved coin, not
a non-pumper. Extending the gate is a precondition for generating the
new labels at all, and it interacts with §9 sequencing constraint #1.

### Exit labels (new)

- `t*_EV` — the executable-peak time: `argmax_t EV(t, Q)` per episode.
  It is **size-dependent**; labels are generated at reference sizes
  `Q ∈ {2%, 10%, 25%}` of the episode's peak quote depth so policies
  can be evaluated at more than one position scale.
- **Harvest window** — the maximal interval around `t*_EV` in which
  both: `EV ≥ (1 − θ) · EV_peak` with `θ ≈ 0.10–0.15`, **and** quote
  depth ≥ 50% of the episode's peak depth. The depth condition is what
  makes the window a *harvest* window rather than a price window: the
  back half of a price plateau with the pool drained is not exitable.
- Snapshot classes derived from the window: `pre-harvest`,
  `in-window`, `post-harvest`, plus time-to-window as a regression
  target.
- Collapse-confirmation time: first time of a ≥ 50% drawdown from peak
  with no recovery within 4h. This is the label the reframed
  `exit_risk` (§8) is validated against — imminent post-peak drawdown,
  not eventual collapse.
- **Second-leg flag — kept, never deleted.** An episode that makes a
  new EV peak > 1.5× after a ≥ 30% drawdown is a second leg, flagged as
  such. Second legs are the cases a naive exit policy loses most on;
  deleting or merging them into the first episode would hide exactly
  the failures the exit evaluator exists to catch.

### Leakage law

Exit and entry features are causal only: every feature value must be
computable from data with timestamps ≤ the decision timestamp. This is
enforced mechanically, not by review — an automated feature/label
timestamp audit runs in CI and fails the build on any feature whose
inputs postdate its label's decision time. Episodes right-censored at
the lake edge (episode still open when the data ends) are excluded
from training labels entirely; they may appear in replay only as
truncated episodes, marked as such.

## 6. Gates and metrics

`scores_validated` is **replaced**. It was a single flag over exactly
four required cells in `validation/status.py` (`_REQUIRED_CELLS`), all
four risk-side, and the harness that feeds it loads `p_ignition_24h`
(`validation/harness.py` L99, L784) but never evaluates it — the entry
half of the old mission had no gate at all. The replacement keeps the
one property of the old gate worth keeping — **strict beat on the live
stratum**, the discipline under which the live-BLACK cell correctly
refused to pass — and applies it to both halves of the actual mission.

- **`entry_validated`** — true only when the entry ranking beats its
  baseline on the **live stratum** under strict-beat: ignition
  precision@k and forward-gain lift (top-group `MFE_6h` hit rate vs
  base rate) from the entry evaluator's cells (§3 items' falsification
  metrics, computed by the harness, not by the signal code). Screening
  and weight windows do not count; only window C, only live stratum,
  only strict beat.
- **`exit_validated`** — true only when the exit policy beats the fixed
  gain-ladder benchmark on **held-out episodes** under strict beat, on
  both **median and p25 Captured Fraction**. Beating the ladder on the
  median while losing on p25 means the policy helps typical trades and
  hurts bad ones; that is a fail, because the bad ones are where the
  money is lost.

Transition, stated plainly: `validation/status.py` reports the two new
flags. `scores_validated` is retired as a gate — it is not kept as a
read-only third flag for a transition release, because a retired gate
that still prints will still be read as a verdict. The collapse-side
cells that fed it do not disappear; they are re-homed as exit-side
diagnostics under the exit evaluator, where collapse information
belongs under the new mission.

Reportable metrics, by side:

- Entry: precision@k/day at the operating k, PR-AUC, forward-gain lift
  vs base rate, median lead time (signal → threshold crossing) for true
  positives. Never accuracy, never ROC-AUC alone at 1% prevalence.
- Exit: median and p25 CF per policy vs each §4 benchmark, regret
  decomposition (timing / impact / early), and CF at each reference
  size `Q`. A CF quoted without its `Q` is incomplete.

## 7. Data-lake preservation

The refactor is a re-aiming of derived artifacts, not a data migration.
Nothing in this section is rebuilt, moved, or pruned by the refactor:

- `data_lake/` — kept as-is. The lake and its label-generation path
  (`data_lake/labels.py`) are where new labels are generated from.
- `ingestion/` — kept as-is. Collection continues uninterrupted through
  the refactor; a gap in collection is the one unrecoverable failure
  mode available to this programme.
- `storage/` — kept as-is, including the migration chain (currently
  ending at `0043_features_retention_runs`).
- `ops/archive` — kept as-is: Parquet raw evidence with its SHA-256
  manifest is the ground truth every label regeneration answers to.
- Market snapshots are the intended label source, but **not yet a
  deep history** (measured 2026-11-28: 26,562 rows / 7,849 assets /
  5 days, ~3.4 points per asset; 99.1% of forecasted assets
  unevaluable for forward gains at that density). The **Parquet
  archive is the deep raw-evidence layer** today. The **features
  table is retention-bounded** (`ops/features_retention.py` prunes
  it); new labels (§5) are therefore generated from snapshots and the
  archive, never from features rows that may already be pruned — and
  generating them at scale is gated on the snapshot-coverage
  precondition (implementation plan, Task 0), which raises snapshot
  density for forecasted/scored assets to the scan cadence. This
  changes no preservation rule in this section: snapshots, archive,
  and lake are all kept verbatim regardless.
- Features machinery, `backtest/runner.py`'s
  `point_in_time_market_rows`, and the `trading/` execution plumbing
  are kept as-is; the replay harness (§3 item 0) builds on
  `point_in_time_market_rows` rather than beside it.

One preservation hazard is created *by* the refactor and is handled in
§9: retention deletes feature rows, and its own docstring admits a
pruned row at a labeled key is unrecoverable. The moment new label
types exist, their keys are labeled keys — the retention keep-set must
learn them before the labels are generated, not after the first prune
run discovers the gap.

## 8. Component dispositions

**Keep as-is** (no mission change): `data_lake/`, `ingestion/`,
`storage/`, `ops/archive`, features machinery,
`backtest/runner.py:point_in_time_market_rows`, `trading/` execution
plumbing. (§7 gives the reasons.)

**Reframe:**

- `pump_physics/engine.py` — the lifecycle state machine
  (`SEEDING → IGNITION → PARABOLIC → SATURATION → COLLAPSE`, plus
  `DEAD`/`RUGGED`/`SURVIVOR`) is monotonic and currently alerts only on
  danger phases. Entry transitions (`SEEDING → IGNITION`,
  `IGNITION → PARABOLIC`) get alerting equal to the danger phases: the
  machine becomes the shared spine of §1, not a risk accessory.
- `scoring/formulas.py:exit_risk` (~L286–288 components) — the formula
  exists; its target changes. It is reframed and revalidated as
  *imminent post-peak drawdown / exit-window closing*, against the §5
  collapse-confirmation label — not against eventual collapse, which a
  harvested position does not care about.
- `forecast/engine.py` peak hazards — phase-conditioned peak hazards
  become the cohort-conditional second-stage exit model (§4). Fix the
  `mean_hours_to_peak` metric bug (~L700: populated from
  `fit.mean_time_to_collapse`) as part of this reframe.
- Collapse model and labels — demoted from gate to input: they feed the
  exit stack's completion layer and exit diagnostics. They are not
  deleted; they are re-homed.
- `validation/harness.py` — gains entry and exit evaluator cells (§6);
  its existing collapse cells become exit-side diagnostics.

**Build new:**

- Continuous forward labels (`MFE_H`, time-to-pump, competing-risk
  events) and exit labels (`t*_EV`, harvest windows, snapshot classes,
  collapse-confirmation, second-leg flag) per §5.
- Point-in-time replay harness (§3 item 0).
- Entry evaluator cells and exit evaluator cells in the harness (§6).
- Exit-window liquidity label: the depth condition of the harvest
  window as a standalone, queryable label (depth ≥ 50% of episode peak
  while EV within θ of peak), so policies can be screened on it without
  recomputing windows.
- Cohort-conditional second-stage exit model (§4), seeded by the
  existing phase-conditioned hazards.

**Discard:**

- `scores_validated` as the sole gate — replaced by `entry_validated` +
  `exit_validated` (§6).
- `scoring/formulas.py:research_priority` (~L303–310) in its current
  form: it subtracts `0.15 · collapse_probability` and zeroes the score
  for BLACK-band coins. Under the new mission that penalizes exactly
  the profile of the best trades — a coin likely to collapse *after*
  mooning is the canonical harvest candidate. Entry ranking is driven
  by `p_ignition` / expected forward gain / hours-to-peak; collapse
  terms move to position management (exit arming and sizing), where
  they change how a trade is handled rather than whether it is seen.
- GREEN-as-safety semantics, everywhere they appear (bands, UI copy,
  reports). GREEN can only mean "no exit signal yet" (§2, principle 10).

## 9. Sequencing

Dependency order. Each step's precondition is the step above it; the
two long poles at the end are parallel-safe with everything else once
their inputs exist.

1. **Retention keep-set fix.** Add the new label types' keys to
   `ops/features_retention.py`'s keep-set **before any new labels are
   generated**. This is sequencing constraint #1 and it is first
   because it is the only step whose failure destroys data: retention
   deletes feature rows, and a pruned row at a labeled key is
   unrecoverable by the retention module's own admission.
2. **Continuous forward labels** (§5) — including the symmetric
   extension of `forward_evidence_sufficient` to gain labels, generated
   from snapshots/archive.
3. **Replay harness** (§3 item 0).
4. **Entry rank composite + falsification tests** (§3 items 1–4, in
   order; item 5 is step 11).
5. **Entry evaluator + `entry_validated` gate** (§6).
6. **Exit labels** — harvest windows, `t*_EV`, classes (§5).
7. **Exit evaluator** — CF machinery and benchmarks (§4, §6).
8. **EV-trailing runner + flow-exhaustion armed ladder** (§4 build
   order 1–3), then the distribution ensemble (build order 4) once its
   holder-delta data dependency is verified.
9. **Exit-window liquidity label** (§8) — after the exit evaluator can
   consume it.
10. **Cohort-conditional exit model** (§4) — phase-conditioned hazards
    promoted to the second stage.
11. **Ranking / surface rewiring** — `research_priority` replacement,
    candidate surfaces, UI language, README/STATE mission text. Done
    after the gates exist, so the surfaces have something true to say.
12. **Smart-money convergence + funding-cluster graph** (§3 items 4–5)
    — the long poles; highest effort, parallel-safe, gated behind
    their case-control tests.

The harvest-window state classifier (§4 build order 5) is deliberately
not in this list as a scheduled step: it is attempted only after steps
8–10 are measured, and only under its episode-split discipline.

**Addendum (2026-11-28) — steps 13–17, scheduled by the implementation
plan as Tasks 13–17.** The list above folded several spec items into
earlier steps or left them implicit; they are now scheduled explicitly,
at the dependency positions noted:

13. **Social-presence prior** (§3 item 3) — after the entry composite
    (step 4) and the forward labels (step 2) it is falsified against.
14. **Buyer-acceleration feature** (§3 item 2's second-difference half)
    — alongside step 4's entry programme; the composite term in step 4
    was specified before this feature existed as a stored feature.
15. **Depth-lead study** (§4 build order 1) — before the exit policy
    (step 8): its median-lead result gates whether the policy's pacing
    layer is enabled by default.
16. **Distribution ensemble + holder-delta verification** (§4 build
    order 4, layers 3 + 5) — the data-dependency verification the
    original step 8 deferred ("once its holder-delta data dependency
    is verified") is this step's first deliverable; the ensemble
    follows it, after step 8's runner + armed ladder exist.
17. **Exit-window liquidity label + cohort-conditional stage** (§5/§8
    label; §4 second stage) — the label half extends step 9 and the
    model half is step 10 made concrete: both are scheduled here as
    one step because the model consumes the label.

Ahead of all of these sits the implementation plan's **Task 0**
(snapshot-coverage precondition, §5/§7): it is step 0 in effect —
labels (step 2), replay (step 3), entry falsification (step 4), and
harvest labels (step 6) all presume a snapshot density the live DB
does not yet have.

## 10. Risks and open questions

- **Pending inputs (measurements in flight).** Two measurements were
  running as this spec was written, and their results land as evidence
  for the entry-gate design in §6: (a) the rank-sum forward validation
  on a fresh window (fixed threshold 0.7546, window from 2026-11-22) —
  the first genuinely forward test of the rank-sum machinery §3 item 1
  re-aims at entry; (b) an entry-fitness probe of the current scores
  (forward max-gain hit rates of today's ranking against the §5 label
  definitions). Neither result is assumed anywhere in this spec; if (b)
  shows the current ranking already carries entry signal, item 1's
  baseline changes and its 1.5× bar is re-argued, not waived.
- **Holder balance deltas.** The distribution ensemble (§4 build order
  4) requires holder *balance deltas* over time. Whether the lake
  stores deltas — or only holder counts and snapshots from which deltas
  can be reconstructed point-in-time — is unverified. If only counts
  exist, top-holder net flow is unmeasurable and the ensemble loses a
  layer; this must be settled before step 8 of §9 is scheduled.
- **Forecasts are silent on the live host.** The newest
  `forecasts.decision_ts` on the live DB is 2026-11-27 09:18 UTC —
  the forecast cycle has not persisted rows since, while scoring
  continued. This is an operational fault to diagnose separately (it
  is not a design question and this refactor does not fix it), and it
  is a **precondition for live entry validation**: until the forecast
  cycle is running again, the §3 freshness check correctly holds
  `entry_validated` at False with reason "stale forecasts."
- **Prevalence vs the live gate.** The strict-beat-on-live discipline
  is inherited from a gate that, on the collapse side, has never
  passed. At ~1% entry prevalence the live-stratum cell sizes may make
  strict beat statistically unreachable for long stretches. Open
  question: minimum live-cell n and the exact strict-beat test for
  `entry_validated` — to be fixed in the implementation plan, with the
  numbers from pending input (b), not by relaxing the discipline in
  advance.
- **Reference-size realism.** Exit labels are generated at
  `Q ∈ {2%, 10%, 25%}` of peak quote depth. Whether 25% is a realistic
  operator position or a stress case only affects how CF results are
  weighted in the gate; the gate's `Q` must be named before the exit
  evaluator is built, not after its first results.
- **Regime dependence.** The social prior's lift (8.94×) and the
  graduation base rate (0.198%, a fast-regime lower bound per the
  source paper's own caveat) are regime-stamped numbers. A regime
  change silently invalidates both the prior and every threshold
  estimated alongside it; per-regime re-estimation is specified (§3
  item 3) but regime *detection* is not designed yet.
- **Second legs and the runner.** The EV-trailing runner will, by
  construction, exit some positions that later make a second leg
  (§5 flag). The regret decomposition counts this as early-exit regret,
  but the acceptable rate is a policy choice the operator has not yet
  been asked to make.
- **Self-deception risk is the main risk.** Every catastrophic failure
  mode available to this programme is a measurement fooling its maker:
  lookahead via restated data, leakage via full-history wallet PnL,
  episode leakage in the classifier, screening-window results quoted
  as evaluations. The defences are structural (three-window discipline,
  BH correction, CI timestamp audit, episode-level splits) and are
  specified as build requirements, not aspirations — but they only
  work if the implementation plan keeps them as gates rather than
  guidelines.
