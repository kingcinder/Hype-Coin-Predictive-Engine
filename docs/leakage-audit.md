# Feature & Label Leakage Audit

_Scope: is any feature value computed from data that wasn't known at its decision
time, or do any labels leak into feature rows? Inspected `features/factory.py`,
`features/definitions.py`, `forecast/labels.py`, `data_lake/labels.py`, and the
forecast training path in `forecast/engine.py`, following up on the dense-label
interpolation pattern that the real-only metrics work already surface._

Audit date / engine: `main`.

## Methodology

For every feature in `FEATURE_NAMES` I traced where its value comes from and whether
every input row is filtered to data **observable at or before the feature's
`decision_ts`**. Two classic vectors were the focus, mirroring the known dense-label
issue:

1. **Features computed from post-decision data** — a value that only exists because
   of events/records that occurred after the decision hour (look-ahead).
2. **Labels seeding feature rows** — label generation writing or mutating values that
   later become model inputs.

The label engines were audited for point-in-time correctness of the *target*
construction (labels may legitimately use forward outcomes — that is the label — but
only when the outcome was knowable at the label's generation time).

## Findings

### 1. [HIGH] `deployer_history_available` is not point-in-time

- `features/factory.py:469` `_deployer_history(session, asset_id)` — the count is
  computed from **all** contracts for the asset that have a `deployer_wallet`, with
  **no `decision_ts` and no `observed_at` filter**.
- Effect: as more contracts are discovered/analyzed over time, every previously
  persisted feature row for that asset retroactively holds a stale (later) count.
  A backtest or retroactive scoring reads a value that reflects the *future* state
  of analysis, not what was known at the decision hour.

### 2. [HIGH] `website_presence` / `github_presence_public` read the mutable Asset record

- `features/factory.py:316-317` build these from the **current** `asset.website_url`
  / `asset.github_url` attributes — the live record, not a snapshot as of
  `decision_ts`.
- Effect: a link added post-mint flips **every historical** feature value for that
  asset to `1`. Classic look-ahead whenever features are (re)built for a past
  decision — including walk-forward backtests and retroactive scoring.
- Contrast: these same static fields are absent from the lake replay block, so a
  `feature_source="lake"` replay and the `sql` path disagree on them over time.

### 3. [HIGH] `collapse_probability_24h` is a model-output feature (feedback loop + skew)

- `features/definitions.py:37` defines it; `factory.py:372` / `:769`
  `_forecast_probability` return the most recent `Forecast` row with
  `decision_ts <= decision_ts`.
- `forecast/engine.py:43` sets `FORECAST_FEATURE_NAMES = FEATURE_NAMES`, so the
  forecast model is trained on **its own previous output** as an input feature.
- Effects:
  - **Feedback loop:** the model predicts `collapse_probability_24h`, which then
    becomes an input to the *next* training run — errors compound instead of
    correcting.
  - **Ordering-sensitive label-adjacent signal:** a `Forecast` used as a feature at
    sample time `ts` was itself fit on labels observed by its training time. When
    `ts` lies inside that earlier model's prediction window, the feature value is
    derived from a model trained on outcomes overlapping the very window this sample
    is labeling. Whether this is a true leak depends on the training/prediction
    ordering, but it is a fragile, easy-to-break boundary.
  - **Train/serve skew:** `LakeFeatureFactory` reconstructs only the market/holder/
    flag block, so `collapse_probability_24h` is `missing` in lake replays but
    populated in live scoring — the model learns with it missing in backtests yet
    sees it live.

### 4. [MED] `rpc_pool_health` uses live in-memory state, not a value as-of `decision_ts`

- `factory.py:753` `_rpc_pool_health` calls `get_rpc_pool(chain_slug).snapshot()` —
  the **current** pool health in this process.
- Effect: for a historical `decision_ts` the feature reflects *now*, not that hour.
  Not point-in-time and not replayable from the archive. It changes under the model
  between a backtest and a live run whenever pool conditions differ.

### 5. [MED] Bootstrap labels read market snapshots without `observed_at` guards

- `forecast/labels.py` `seed_labels_at_feature_timestamps` builds the entry price
  (`ts <= feature_ts`) and the forward-window prices (`feature_ts < ts <= feature_ts
  + window`) from queries that filter `ts` but **not** `observed_at`.
- Contrast: `LabelEngine.generate` routes through `point_in_time_market_rows`
  (`observed_at <= decision_ts`) and refuses to write a label until the full forward
  window is in the past. The bootstrap path does neither.
- Impact: the target itself is allowed to use forward *prices*, but these labels also
  rely on price data the system had **not yet observed** at the label's
  `observed_at`, breaking the "this was a knowable outcome" invariant that the
  real-only test readout depends on. Feature-aligned labels are classified as "real"
  (`_is_dense_label_source` only excludes `dense-labels:`), so contaminated ones
  currently pollute the real-only metrics.

### 6. [LOW] Dense-label entry price can be interpolated from a future snapshot

- `data_lake/labels.py` `_interpolate_price`: when `current` falls between two
  observed snapshots, the entry (the denominator for peak/trough %) is interpolated
  using the **immediately-following** snapshot — i.e. data at/after `current`.
- Impact: marginally understates ignition/collapse magnitudes on interpolated labels.
  Target-side only → **label noise, not feature leakage**, but it makes the already
  softer dense labels noisier.

### 7. [LOW] Dense-label forward windows are computed from raw future snapshots

- `data_lake/labels.py` `generate_dense_labels`: `future_prices` come from all
  actual snapshots with `ts <= current + window`, without an `observed_at` cutoff.
  Combined with #5, the same not-yet-observed price data feeds both bootstrap and
  dense label construction.

## Confirmed clean (reassuring)

- **Labels never write feature rows.** The "labels seeding feature rows" concern from
  this audit's brief is **not present**: no label-generation path inserts or mutates a
  `Feature` row. The only label↔feature coupling is via the shared market data and the
  model-output feature (#3).
- `LabelEngine.generate` is correctly point-in-time: it goes through
  `point_in_time_market_rows` and refuses labels until `decision + forward_hours <=
  generation_ts`, so it never leaks the future into the target.
- Most query-based features carry `observed_at <= decision_ts` (and often
  `ts <= decision_ts`) guards and are genuinely as-of the decision hour:
  `holder_count`/`holder_growth`/`top_holder_concentration`, `suspicious_contract_flags`,
  `ignition_signal`, `liquidity_withdrawal_signal`, `lp_removal_signal`,
  `catalyst_proximity_hours` (a scheduled future catalyst is knowable now),
  `narrative_cluster_growth_7d`, `shill_channel_diversity`, `prelaunch_narrative_velocity`,
  `kol_velocity`, `github_star_velocity`, `hf_download_velocity`, `recidivism_score`,
  `prelaunch_priority`, `lifecycle_phase`, `mention_velocity`, `narrative_acceleration`.
- The real-only vs blended test readout and the `forecast_calibration` / real-only
  usage gate already bound the influence of interpolated labels (see
  `docs/validation.md` "Calibration-Bias Guard Proof" / "Real-Only Usage Gate Proof").

## Recommended remediation (by priority)

- **P1 — Kill the model-output feedback (#3):** do not feed `collapse_probability_24h`
  into the forecast feature matrix. Either drop it from `FORECAST_FEATURE_NAMES` for
  the forecast model, or persist each prediction as a first-class series with a strict
  `decision_ts` and, if kept, consume only values generated strictly before the sample
  time (and accept the train/serve skew honestly).
- **P1 — Snapshot asset metadata (#2):** capture `website_presence` /
  `github_presence_public` (and deployer-history availability, #1) at feature-build time
  with the decision hour, or force them to `missing`/`0` for decisions that predate the
  record's value. This removes the retroactive-mutation look-ahead.
- **P2 — Make `deployer_history_available` (#1) and `rpc_pool_health` (#4) point-in-time:**
  last-known-value at `decision_ts` (persist the history) or exclude them from
  historical replay.
- **P2 — Restore the knowable-at-generation invariant in bootstrap labels (#5):** add
  `observed_at` cutoffs (relative to `decision_ts` at generation time) to the entry and
  forward-window queries in `seed_labels_at_feature_timestamps`, matching
  `LabelEngine.generate`.
- **P3 — Tighten dense-label entry interpolation (#6, #7):** interpolate the entry only
  from snapshots at/before `current`, or document the slight underestimate as accepted
  target noise.
