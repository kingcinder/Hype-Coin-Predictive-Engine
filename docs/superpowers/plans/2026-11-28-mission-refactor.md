# Hype-Coin Mission Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the engine from a collapse-avoidance scorer into a two-front mission system — Front 1 (Entry / "New Moon") ranks coins by predicted forward gain before the rise; Front 2 (Exit / "Harvest") times exits near the executable peak — with continuous forward labels, point-in-time replay, and separate `entry_validated` / `exit_validated` gates replacing the single `scores_validated` flag.

**Architecture:** The data lake and point-in-time discipline stay untouched and additive-only. New knowledge enters as new `label_type` rows in the existing `labels` table (continuous forward-path and harvest labels alongside the binary `ignition`/`collapse` labels), new evaluators in `validation/harness.py` emitting the existing `MetricCell` shape, and two new gate functions in `validation/status.py`. Entry ranking becomes a rank-sum composite in `scoring/entry_rank.py` feeding a reframed `research_priority`; exit timing becomes a rule-based policy in `trading/exit_policy.py` scored by the exit evaluator against ladder / fixed-horizon / clairvoyant benchmarks.

**Tech Stack:** Python 3.12, SQLAlchemy ORM over SQLite/Postgres, NumPy, HistGradientBoosting (existing forecast stack), pytest, ruff, mypy. No new dependencies.

**Spec:** docs/mission-refactor-2026-11-28.md (§1 Mission, §2 Principles, §3 Entry, §4 Exit, §5 Labels, §6 Gates and metrics, §7 Data-lake preservation, §8 Component dispositions, §9 Sequencing, §10 Risks)

## Global Constraints

- Data lake is additive-only: no table drops, no column drops, no retention-policy weakening. New label types are new rows in `labels`; new tables (Task 12 only — plus Task 16 Branch B if its holder-delta verification forces delta capture) are new migrations continuing the `storage/migrations/versions/` sequence after `0043_features_retention_runs.py`.
- Features are causal: every feature, label, and replay read is gated on `observed_at <= decision_ts` (the `point_in_time_market_rows` discipline in `backtest/runner.py`). A timestamp audit test accompanies any new read path.
- Python 3.12; `ruff check`, `ruff format --check`, and `mypy` over the CI package set stay clean; the full pytest suite is green at every commit.
- Every task ends with a commit step. Commits are small and named for the task.
- Label thresholds and policy constants live as `Settings` fields in `common/config.py`, following the existing annotated-default pattern (`forecast_forward_hours: int = 24`, `features_retention_resolving_hours: float = 72.0`). No magic numbers in label/policy code.
- Label values: binary labels keep the `"1"`/`"0"` `label_value` convention; continuous labels store `f"{value:.6f}"` in the same `String(256)` column. No schema change for labels.
- Existing binary labels (`ignition`, `collapse`), `risk_engine/outcomes.py` (`LABEL_VERSION = "v3_bounded"`), and the collapse-side harness cells keep working unchanged until Task 11 rewires the surface; nothing in Tasks 1–10 removes a collapse-side behavior.
- Snapshot coverage is a precondition, not an assumption (Task 0).
- Three-window discipline (spec §2, principle 7): any signal screening runs on window A, weight fitting on window B, and evaluation on untouched window C, with Benjamini–Hochberg correction across all screened features. This binds Tasks 5, 13, and 14 explicitly, and any later task that screens features. A number quoted from window A or B is a hypothesis, never a result.

## Review Focus

The five failure inputs most likely to bite, each pinned to a test in its owning task:

1. **Thin-window fake "no pump" labels** — a decision point with two snapshots in 24h reads as `forward_max_gain_pct ≈ 0` ("no pump") when the window was simply unobserved, training the entry model against ghosts. Owning task: **Task 2**, test `test_forward_labels.py::test_small_gain_label_censored_when_forward_evidence_thin` (gain-side density censoring, symmetric with collapse=0).
2. **Snapshot-split leakage across train/test** — the same asset's snapshots straddling the eval cutoff let near-duplicate rows sit on both sides, inflating entry precision. Owning task: **Task 6** (entry evaluator consumes the harness's existing embargoed walk-forward split — `EMBARGO_HOURS = 48`, `PURGE_HOURS = 24` in `validation/harness.py` — never a fresh random split), test `test_entry_validation.py::test_entry_cells_respect_embargo_split`.
3. **Retention pruning new label keys** — the features-retention pass deletes feature rows at keys whose forward/harvest labels have not been written yet, silently destroying the new training pool before it exists (the Nov 22 prune destroyed forward-validation coverage exactly this way). Owning task: **Task 1**, test `test_features_retention.py::test_prune_keeps_mission_label_keys_before_labels_exist`.
4. **Vendor-restated history in training** — a backfilled or restated snapshot (`observed_at` after the decision time) rewrites what "was known" and leaks the future into labels and replay. Owning tasks: **Task 2**, test `test_forward_labels.py::test_restated_snapshot_does_not_change_forward_labels`; **Task 4**, test `test_replay_harness.py::test_replay_ignores_snapshots_observed_after_decision`.
5. **Second-leg episodes deleted as noise** — a coin that dumps 50%+ off its peak and then runs again is one episode with a second leg, not noise to clean; deleting or splitting it teaches the harvest model that post-crash recoveries don't happen. Owning task: **Task 7**, test `test_harvest_labels.py::test_second_leg_episode_retained_and_flagged`.

---

## File Structure

New files:

- `ingestion/coverage.py` — snapshot-coverage measurement: per-asset snapshot density + `forward_evidence_sufficient` pass share (Task 0).
- `scripts/snapshot_coverage_report.py` — coverage report writer; the Task 0 gate artifact (Task 0).
- `backtest/replay.py` — causal point-in-time replay: entries priced at the next snapshot plus impact, forward paths attached (Task 4).
- `scoring/entry_rank.py` — entry rank-sum composite v1 with wash-lite adjustment (Task 5).
- `forecast/harvest.py` — harvest (executable-peak) label computation from price + depth paths (Task 7).
- `trading/exit_policy.py` — rule-based exit policy v1 (ladder floor, flow-armed depth-paced tranches, EV-trail runner, LP-removal override) plus the replay row builder that feeds the exit evaluator (Task 9).
- `scripts/falsify_entry_rank.py` — entry-rank falsification run + report writer (Task 5).
- `scripts/case_control_smart_money.py` — smart-money convergence case-control gate (Task 12).
- `scripts/social_prior_falsification.py` — social-presence contingency-table falsification run (Task 13).
- `scripts/depth_lead_study.py` — depth-lead study + report (Task 15).
- Tests: `tests/test_snapshot_coverage.py`, `tests/test_forward_labels.py`, `tests/test_hazard_peak_metric.py`, `tests/test_replay_harness.py`, `tests/test_net_inflow_feature.py`, `tests/test_entry_rank.py`, `tests/test_entry_validation.py`, `tests/test_harvest_labels.py`, `tests/test_exit_validation.py`, `tests/test_exit_policy.py`, `tests/test_research_priority_reframe.py`, `tests/test_mission_status_surface.py`, `tests/test_smart_money_gate.py`, `tests/test_social_prior.py`, `tests/test_buyer_acceleration.py`, `tests/test_depth_lead_study.py`, `tests/test_distribution_features.py`, `tests/test_exit_window_labels.py`.

Modified files:

- `ingestion/service.py`, `ingestion/scheduler.py` — forecasted/scored assets snapshotted on the scan cadence (Task 0).
- `ops/features_retention.py` — mission-horizon keep rule (Task 1).
- `forecast/labels.py` — continuous forward labels + shared `forward_path` helper (Task 2), harvest label wiring (Task 7).
- `data_lake/labels.py` — same continuous labels on the dense/replay paths (Task 2).
- `forecast/hazard.py`, `forecast/engine.py` — peak-fit mean-time alias and its use (Task 3).
- `features/definitions.py`, `features/factory.py` — `net_inflow_ratio` feature (Task 5); `unique_buyers_acceleration` feature (Task 14); distribution features (Task 16).
- `ingestion/normalizers.py` — profile-link extraction extended to Telegram / X presence (Task 13).
- `scoring/entry_rank.py` — social-presence prior multiplier (Task 13).
- `forecast/harvest.py` — exit-window liquidity labels (Task 17).
- `trading/exit_policy.py` — distribution accelerate votes (Task 16); cohort-hazard completion input (Task 17).
- `validation/harness.py` — forward-label loader, entry evaluator (Task 6), exit evaluator (Task 8).
- `validation/status.py` — `entry_validation_status` (Task 6), `exit_validation_status` (Task 8), `scores_validated` retirement and combined surface (Task 11).
- `scoring/formulas.py` — `research_priority` reframe (Task 10).
- `pump_physics/engine.py` — entry-phase (IGNITION/PARABOLIC) transition alerts (Task 11).
- `ui/trading_view.py`, `api/trading_routes.py` — two-flag status surface (Task 11).
- `common/config.py` — new `Settings` fields in Tasks 0, 1, 5, 6, 7, 9, 13, 17.
- `README.md`, `STATE.md`, `docs/blueprint.md` — mission language per spec §8 (Task 11).
- `storage/models.py` + one migration — point-in-time wallet-PnL table (Task 12, gated).

---

### Task 0: Snapshot-coverage precondition

Spec §5, §7 (as amended 2026-11-28). Everything downstream presumes forward windows are actually observed. Measured on the live DB (2026-11-28): `market_snapshots` held **26,562 rows / 7,849 assets over 5 days (~3.4 points per asset)**, and **99.1% of forecasted assets were unevaluable for forward gains** — the density rule in `forecast/labels.py::forward_evidence_sufficient` requires ≥ ⌈window / 6h⌉ snapshots in the window (⌈24h / 6h⌉ = **4** for the 24h window) **including one in the final quartile**, and almost no live window has them. Snapshots are written by `storage/repository.py::insert_market_snapshot_once`, called from the pair-ingest path in `ingestion/service.py` (~line 1448, inside `IngestionService.run_once`'s cycle, line 279) and from `mempool/evm.py`; source payloads come from `ingestion/source_clients.py` (DexScreener / Birdeye / GeckoTerminal clients); the cycle cadence is `Settings.scan_interval_seconds` (300s) via `ingestion/scheduler.py`. The gap: pairs enter the snapshot path when they are polled as pairs, but the set of assets that receive forecasts/scores is not the set guaranteed a snapshot every cycle — so decision points accrue without forward observations. This task measures that precisely, then closes it, **before** any label task runs.

**Files:**
- Create: `ingestion/coverage.py`
- Create: `scripts/snapshot_coverage_report.py` (entrypoint pattern per `tests/test_script_entrypoint_pattern.py`: `main()` + `if __name__ == "__main__"`)
- Modify: `ingestion/service.py` (`run_once` cycle: the forecasted/scored asset set is snapshot-polled every cycle), `ingestion/scheduler.py` (only if the fix needs a distinct coverage pass on the same interval — prefer wiring into `run_once`)
- Modify: `common/config.py` (Settings, beside `scan_interval_seconds` ~line 119: `snapshot_coverage_min_pass_share: float = 0.80`)
- Test: `tests/test_snapshot_coverage.py` (create)

**Interfaces:**
- Consumes: `forecast.labels.forward_evidence_sufficient(timestamps, *, start, window)` (existing, `forecast/labels.py:23`); `models.MarketSnapshot` (`pair_id`, `ts`, `observed_at`, `source_id`) joined via `models.Pair` to assets, exactly as `ingestion/service.py` already joins them (~lines 881–884, 1718–1720); `models.Forecast` / scores tables for the forecasted/scored asset set (`forecasts.decision_ts` per asset); `Settings.forecast_forward_hours` (24) as the window.
- Produces:
  - ```python
    # ingestion/coverage.py
    @dataclass(frozen=True)
    class CoverageReport:
        as_of: datetime
        window_hours: int
        assets_measured: int                 # forecasted/scored assets with a decision point in scope
        assets_meeting_density: int          # assets with >= 1 decision point passing forward_evidence_sufficient
        pass_share: float                    # assets_meeting_density / assets_measured (0.0 when none)
        median_snapshots_per_asset_day: float
        p10_snapshots_per_asset_day: float

    def snapshot_coverage(
        session: Session, *, settings: Settings, as_of: datetime
    ) -> CoverageReport: ...
    ```
    Density is judged per decision point with the **same** rule the labels use — `forward_evidence_sufficient(snapshot_ts_list, start=decision_ts, window=timedelta(hours=settings.forecast_forward_hours))`, i.e. ≥ ⌈24h / 6h⌉ = 4 snapshots in the 24h after the decision, one of them in the final quartile (18–24h). No parallel, weaker coverage definition.
  - `scripts/snapshot_coverage_report.py`: writes `reports/snapshot-coverage-<YYYYMMDD>.json` + `.md` from `snapshot_coverage(...)`; exit 0 iff `pass_share >= settings.snapshot_coverage_min_pass_share`, else 1 (report written either way).
  - Cadence fix contract: after this task, every asset that received a forecast or a score in a scan cycle has a market snapshot written in that same cycle (`insert_market_snapshot_once` via the existing pair path — extend the polled set, do not build a second snapshot writer).

- [ ] **Step 1: Write the failing tests**

```python
def test_coverage_counts_density_with_the_label_rule(session):
    # Asset A: decision at T0, snapshots at +1h, +7h, +13h, +20h
    # (4 in window, one in the final quartile) -> meets density.
    # Asset B: decision at T0, snapshots at +1h, +2h, +3h
    # (3 in window, none in the final quartile) -> does not.
    ...
    report = snapshot_coverage(session, settings=get_settings(), as_of=T0 + 48 * H)
    assert report.assets_measured == 2
    assert report.assets_meeting_density == 1
    assert report.pass_share == pytest.approx(0.5)


def test_coverage_ignores_snapshots_observed_after_as_of(session):
    # A snapshot backfilled with observed_at after as_of must not
    # retroactively make a thin window dense (point-in-time or it
    # didn't happen).
    ...


def test_scan_cycle_snapshots_forecasted_assets(session):
    # Drive one run_once cycle with a forecasted asset whose pair is
    # not in the default poll set; assert a market_snapshots row for
    # its pair exists with ts inside the cycle.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_snapshot_coverage.py -v`
Expected: FAIL — `ingestion/coverage.py` does not exist; the scan-cycle test finds no snapshot row for the forecasted asset.

- [ ] **Step 3: Implement measurement, then the cadence fix**

First `ingestion/coverage.py` + the report script (measurement only). Run the script against a local copy of the live DB and record the baseline verbatim in the report and in `STATE.md` (expected order of magnitude from the 2026-11-28 probe: ~1% pass share — the measured number governs, not this expectation). Then the cadence fix in `ingestion/service.py::run_once`: the cycle's poll set gains every asset with a forecast or score in the current cycle, snapshotted through the existing `insert_market_snapshot_once` pair path; keep the write inside the cycle's existing commit discipline (the service already commits before provider round-trips to avoid holding the SQLite write lock — follow that pattern).

- [ ] **Step 4: Run tests, then the coverage gate**

Run: `pytest tests/test_snapshot_coverage.py tests/test_ingestion.py tests/test_service.py -v`
Expected: PASS.
Run: `python scripts/snapshot_coverage_report.py` against the live DB (or a same-day copy) after one full day of fixed-cadence collection.
Expected: exit 0 (`pass_share >= 0.80`) — the gate is a *live-data* gate; it cannot pass on the day the fix lands, because the 24h windows it judges need a day to accrue.

- [ ] **Step 5: Commit**

```bash
git add ingestion/coverage.py scripts/snapshot_coverage_report.py ingestion/service.py ingestion/scheduler.py common/config.py tests/test_snapshot_coverage.py reports/snapshot-coverage-*.json reports/snapshot-coverage-*.md STATE.md
git commit -m "feat(ingestion): snapshot-coverage measurement + forecasted assets snapshotted on scan cadence (Task 0 precondition)"
```

**Definition of Done (binding on later tasks):** until the coverage gate passes on live data, Tasks 2/4/5/7 must report expected evaluable-n and may not be declared done on synthetic fixtures alone.

---

### Task 1: Retention keep-set protection for mission label keys

Spec §7, §5. The retention pass (`ops/features_retention.py::build_keep_keys`) keeps labeled keys forever (Rule 1: `SELECT DISTINCT asset_id, ts FROM labels`) and unlabeled keys only inside the pending (24h) + resolving (72h) windows (Rules 2–3). Forward labels resolve inside `forecast_forward_hours` (24h), but harvest labels (Task 7) resolve only when an episode completes — well past 72h. Without this task, the first retention pass after Task 2 lands prunes the feature rows the harvest labels will need. This task runs **before** any new label type exists.

**Files:**
- Modify: `common/config.py` (Settings, beside the `features_retention_*` block ~line 333)
- Modify: `ops/features_retention.py` (`build_keep_keys`, after Rule 5)
- Test: `tests/test_features_retention.py` (extend; existing `engine`/`settings` fixtures and `_seed`/`_keys`/`_count` helpers)

**Interfaces:**
- Consumes: existing `build_keep_keys(conn: Connection, *, settings: Settings, as_of: str) -> int` (`ops/features_retention.py:137`) and the test-facing entry point `run_features_retention(engine, *, settings: Settings)` used throughout `tests/test_features_retention.py` (seed idiom there: `T0` base time, `H = timedelta(hours=1)`).
- Produces:
  - `Settings.features_retention_mission_horizon_hours: float = 168.0`
  - `ops.features_retention.MISSION_LABEL_TYPES: tuple[str, ...]` — exactly `("forward_max_gain_pct", "forward_max_drawdown_pct", "hours_to_peak", "hours_to_trough", "executable_peak_gain_s02", "executable_peak_gain_s10", "executable_peak_gain_s25", "harvest_window_start_hours", "collapse_confirmed", "second_leg")` (Tasks 2 and 7 define these constants with these exact strings).
  - Keep Rule 6 inside `build_keep_keys`: a feature key with an entry price and ≥1 forward price (reuse `_entry_exists_sql('f')` / `_future_exists_sql('f', forward_hours=...)`) whose `decision_ts` is within `features_retention_mission_horizon_hours` of `as_of` is kept even with **no** row in `labels` yet.

- [ ] **Step 1: Write the failing tests**

```python
def test_prune_keeps_mission_label_keys_before_labels_exist(engine, settings):
    # Feature key 120h old (past pending 24h + resolving 72h), entry +
    # forward prices present, ZERO rows in labels for it. Under the old
    # rules it prunes; under Rule 6 (mission horizon 168h) it must survive.
    # Seed it by extending _seed with a labelable 120h-old key.
    seeded = _seed(engine)
    run_features_retention(engine, settings=settings)
    assert (seeded["assets"]["A"], T0 - 120 * H) in _keys(engine)


def test_prune_still_prunes_beyond_mission_horizon(engine, settings):
    # Same labelable shape at 240h old: outside the 168h horizon, pruned.
    seeded = _seed(engine)
    run_features_retention(engine, settings=settings)
    assert (seeded["assets"]["A"], T0 - 240 * H) not in _keys(engine)


def test_mission_label_types_match_label_constants():
    from forecast import labels as forecast_labels
    from data_lake import labels as lake_labels

    named = set(features_retention.MISSION_LABEL_TYPES)
    defined = {
        forecast_labels.LABEL_FORWARD_MAX_GAIN,
        forecast_labels.LABEL_FORWARD_MAX_DRAWDOWN,
        forecast_labels.LABEL_HOURS_TO_PEAK,
        forecast_labels.LABEL_HOURS_TO_TROUGH,
    }
    # Until Tasks 2/7 land the forecast constants do not exist; this test is
    # written now against the pinned strings and xfail-marked in Step 2.
    assert defined <= named
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_features_retention.py -v -k mission`
Expected: FAIL — no Rule 6 (5-day key pruned); the constants test fails on missing `MISSION_LABEL_TYPES`. Mark `test_mission_label_types_match_label_constants` with `@pytest.mark.xfail(strict=True, reason="forecast constants land in Task 2")` in this task; Task 2 removes the marker.

- [ ] **Step 3: Implement**

Add the Settings field; add `MISSION_LABEL_TYPES` beside `KEEP_TABLE`/`COMPONENT`; add Rule 6 to `build_keep_keys` following the Rule 3 SQL shape (entry + future `EXISTS`, `datetime(f.decision_ts) >= datetime(:as_of, '-{mission:g} hours')`, no labels-existence condition). **Expectation audit:** Rule 6 changes outcomes for any pre-existing test key that is labelable (entry + forward prices) and younger than 168h — audit every pre-existing assertion in `tests/test_features_retention.py` against Rule 6; where a key now survives by design, update the assertion in this commit with a comment naming Rule 6 (do not weaken Rule 6 to fit the old expectations).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_features_retention.py -v`
Expected: PASS, including all pre-existing retention tests (idempotency, caps, dry-run).

- [ ] **Step 5: Commit**

```bash
git add common/config.py ops/features_retention.py tests/test_features_retention.py
git commit -m "feat(retention): keep mission-label keys for 168h before labels exist"
```

---

### Task 2: Continuous forward labels (forward path, censored symmetrically)

Spec §5. Today `forecast/labels.py::LabelEngine.generate` and `seed_labels_at_feature_timestamps` compute `peak_pct`/`trough_pct` from the forward window and then **discard** everything except the binary threshold crossings; `forecast/engine.py::_peak_hours`/`_trough_hours` recompute argmax/argmin timing separately for the hazard fits. This task captures the full forward path once — max gain, max drawdown, hours to peak, hours to trough — as continuous label types, from all three generation paths (`LabelEngine.generate`, `seed_labels_at_feature_timestamps`, `data_lake/labels.py::generate_dense_labels` + `replay_historical_labels`).

**Files:**
- Modify: `forecast/labels.py`
- Modify: `data_lake/labels.py` (mirror constants; it already imports `forward_evidence_sufficient` from `forecast.labels` — import the new helper the same way)
- Test: `tests/test_forward_labels.py` (create), `tests/test_label_evidence_density.py` (extend)

**Interfaces:**
- Consumes: `forward_evidence_sufficient(timestamps: Iterable[datetime], *, start: datetime, window: timedelta) -> bool` (existing, `forecast/labels.py:23`); `upsert_label(session, *, asset_id, ts, label_type, label_value, decision_ts, source) -> bool` (existing); `point_in_time_market_rows(session, *, asset_id, decision_ts)` from `backtest/runner.py`.
- Produces (exact names; Tasks 4, 6, 7 consume these):
  - `forecast.labels.LABEL_FORWARD_MAX_GAIN = "forward_max_gain_pct"`, `LABEL_FORWARD_MAX_DRAWDOWN = "forward_max_drawdown_pct"`, `LABEL_HOURS_TO_PEAK = "hours_to_peak"`, `LABEL_HOURS_TO_TROUGH = "hours_to_trough"`
  - ```python
    @dataclass(frozen=True)
    class ForwardPath:
        max_gain_pct: float        # max(future)/entry - 1
        max_drawdown_pct: float    # min(future)/entry - 1 (<= 0)
        hours_to_peak: float       # hours from decision to argmax price
        hours_to_trough: float     # hours from decision to argmin price

    def forward_path(
        entry_price: float,
        start: datetime,
        rows: list[tuple[datetime, float]],
    ) -> ForwardPath | None:  # None when rows is empty
    ```
  - `LabelEngine.generate(...)` and `seed_labels_at_feature_timestamps(...)` return counts dicts extended with `"forward_max_gain"`, `"forward_max_drawdown"`, `"hours_to_peak"`, `"hours_to_trough"` keys; dense path counts likewise.
  - Censoring rule (symmetric with collapse=0): a `forward_max_gain_pct` value **below** `settings.forecast_ignition_threshold` is a "no pump" claim and is written only when `forward_evidence_sufficient(...)` passes for the same window; values at/above the threshold are never gated. `hours_to_peak`/`hours_to_trough`/`forward_max_drawdown_pct` are written whenever their sibling gain label is written (they are measurements, not claims). Continuous `label_value` is `f"{value:.6f}"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_forward_path_captures_peak_trough_and_timing():
    start = datetime(2026, 11, 20, tzinfo=UTC)
    rows = [
        (start + timedelta(hours=2), 1.50),
        (start + timedelta(hours=5), 3.00),   # peak: +200% at 5h
        (start + timedelta(hours=9), 0.40),   # trough: -60% at 9h
        (start + timedelta(hours=12), 1.10),
    ]
    path = forward_path(1.00, start, rows)
    assert path.max_gain_pct == pytest.approx(2.0)
    assert path.max_drawdown_pct == pytest.approx(-0.6)
    assert path.hours_to_peak == pytest.approx(5.0)
    assert path.hours_to_trough == pytest.approx(9.0)


def test_small_gain_label_censored_when_forward_evidence_thin(session):
    # Two snapshots in a 24h window, max gain +3% (< ignition threshold):
    # the "no pump" claim is NOT earned -> no forward_max_gain_pct row,
    # mirroring the collapse=0 density rule.
    ...
    assert label_value(session, asset_id, ts, LABEL_FORWARD_MAX_GAIN) is None


def test_large_gain_label_written_despite_thin_evidence(session):
    # Same thin window, one snapshot at +250%: positives are never gated.
    ...
    assert label_value(session, asset_id, ts, LABEL_FORWARD_MAX_GAIN) == "2.500000"


def test_restated_snapshot_does_not_change_forward_labels(session):
    # A snapshot with observed_at AFTER the label-generation decision_ts
    # (vendor-restated history) must not create or alter forward labels.
    ...
```

Plus, in `tests/test_label_evidence_density.py`: a gain-side case mirroring each existing collapse-side density case (minimum count, final-quartile requirement).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_forward_labels.py tests/test_label_evidence_density.py -v`
Expected: FAIL — `ForwardPath`/`forward_path` and the new label constants do not exist.

- [ ] **Step 3: Implement**

In `forecast/labels.py`: constants, `ForwardPath`, `forward_path` (single pass, argmax/argmin by price with earliest-ts tie-break). In `LabelEngine.generate`, replace the inline `peak_pct`/`trough_pct` computation with `forward_path(entry, decision, [(ensure_utc(r.ts), float(r.price_usd)) for r in future_rows])` and upsert the four continuous labels beside the binary ones, applying the censoring rule to the gain label. Same replacement in `seed_labels_at_feature_timestamps` (its `future_snaps` list) and in `data_lake/labels.py` (`generate_dense_labels`, `replay_historical_labels`; dense labels keep the `dense-labels:` source prefix and the never-overwrite-real rule in `upsert_label`). Remove the Task 1 xfail marker on `test_mission_label_types_match_label_constants`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_forward_labels.py tests/test_label_evidence_density.py tests/test_features_retention.py tests/test_forecast.py tests/test_label_bootstrap.py tests/test_lake_features.py -v`
Expected: PASS. Existing binary-label counts in older tests unchanged (new counts keys are additive).

- [ ] **Step 5: Commit**

```bash
git add forecast/labels.py data_lake/labels.py tests/test_forward_labels.py tests/test_label_evidence_density.py tests/test_features_retention.py
git commit -m "feat(labels): persist continuous forward path (max gain/drawdown, hours to peak/trough)"
```

---

### Task 3: Fix the peak-hazard metric mislabel

Spec §8 (forecast dispositions). `forecast/hazard.py::HazardFit` has one mean-time field, `mean_time_to_collapse`, and the peak fits (`ForecastEngine._fit_peak_hazard`, `_fit_phase_peak_hazards`) store **time-to-peak** means in it. `forecast/engine.py` then emits `hazard.{slug}.mean_hours_to_peak = fit.mean_time_to_collapse` (line ~700) and `_predict` gates peak-hazard selection on `phase_peak.mean_time_to_collapse is not None` (line ~1379) — correct arithmetic, wrong name, and any future reader (or the Task 6 lead-time cells) will wire the collapse mean into a peak slot. Give the peak fit its own attribute name and make the peak paths use it.

**Files:**
- Modify: `forecast/hazard.py` (`HazardFit`, `DiscreteHazardModel.expected_hours`)
- Modify: `forecast/engine.py` (metric emission ~lines 695–700; `_predict` hazard selection ~lines 1366–1394)
- Test: `tests/test_hazard_peak_metric.py` (create)

**Interfaces:**
- Consumes: `DiscreteHazardModel(forward_hours: int).fit(times_hours: list[float], events: list[bool]) -> HazardFit`; `ForecastModel.hazard` / `.peak_hazard` / `.hazards_by_phase` / `.peak_hazards_by_phase` (existing dataclass fields, `forecast/engine.py:344`).
- Produces:
  - `HazardFit.mean_time_to_peak: float | None` — a property alias returning `mean_time_to_collapse` (the fit is generic; the alias names the peak reading at peak call sites without breaking the frozen dataclass's positional construction or pickled fits).
  - Peak call sites in `forecast/engine.py` read `.mean_time_to_peak` (metric `hazard.{slug}.mean_hours_to_peak`, the `_predict` phase-peak `is not None` gate, and `DiscreteHazardModel.expected_hours(peak_hazard, p_ignition)` — `expected_hours` itself stays name-neutral).

- [ ] **Step 1: Write the failing test**

```python
def test_peak_metric_uses_peak_fit_mean_not_collapse_fit_mean(session):
    # Train/predict with samples whose collapses happen late (mean ~20h)
    # and peaks happen early (mean ~3h). The emitted
    # hazard.*.mean_hours_to_peak and Forecast.expected_hours_to_peak must
    # track the PEAK fit (~3h), not the collapse fit (~20h).
    ...
    assert forecast.expected_hours_to_peak == pytest.approx(3.0, abs=1.5)
```

Build the fixture on the existing `tests/test_forecast.py` seeding helpers; if a full train cycle is too heavy, construct `ForecastModel` directly with two hand-built `HazardFit`s (collapse mean 20.0, peak mean 3.0) and drive `_predict` — that is the regression: `_predict` must read the peak fit for `expected_hours_to_peak`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_hazard_peak_metric.py -v`
Expected: FAIL — `HazardFit` has no `mean_time_to_peak` attribute.

- [ ] **Step 3: Implement**

Add the alias property to `HazardFit`; switch the peak call sites in `forecast/engine.py` to it. Do not rename `mean_time_to_collapse` (persisted metrics and phase fits use it positionally).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hazard_peak_metric.py tests/test_forecast.py tests/test_forecast_inference.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forecast/hazard.py forecast/engine.py tests/test_hazard_peak_metric.py
git commit -m "fix(forecast): peak-hazard mean read through mean_time_to_peak, not the collapse field"
```

---

### Task 4: Point-in-time replay harness

Spec §3, §6. `backtest/runner.py` already replays causally (`point_in_time_market_rows` gates `ts <= decision_ts AND observed_at <= decision_ts`; `build_replay_features` never persists), but it prices entries at the decision snapshot and computes only binary hit/miss. The mission needs a reusable replay that prices entry at the **next** snapshot with market impact, and attaches Task 2's continuous forward path (MFE labels) to every entry. New module beside the runner — `backtest/replay.py` — so both falsification (Task 5) and exit scoring (Task 9) consume one causal path.

**Files:**
- Create: `backtest/replay.py`
- Test: `tests/test_replay_harness.py` (create)

**Interfaces:**
- Consumes: `backtest.runner.point_in_time_market_rows(session, *, asset_id, decision_ts) -> list[models.MarketSnapshot]`; `trading.costs.simulate_buy(*, size_sol: Decimal, quote_price: Decimal, slip_pct: Decimal, fee_pct: Decimal, priority_sol: Decimal, price_native: Decimal | None = None) -> Fill` (`.effective_price` is the impact-adjusted entry price); `forecast.labels.forward_path(entry_price, start, rows) -> ForwardPath | None` and `forward_evidence_sufficient(...)` (Task 2); `common.config.Settings` (`forecast_forward_hours`).
- Produces:
  - ```python
    @dataclass(frozen=True)
    class ReplayEntry:
        asset_id: int
        decision_ts: datetime
        entry_ts: datetime          # first snapshot strictly after decision_ts
        entry_price: float          # that snapshot's price, impact-adjusted via simulate_buy
        path: ForwardPath           # forward path from entry_price over the window
        forward_rows: tuple[tuple[datetime, float], ...]  # (ts, price) in (entry_ts, entry_ts + window]

    def iter_replay_entries(
        session: Session,
        *,
        asset_ids: list[int],
        start: datetime,
        end: datetime,
        step_hours: float = 1.0,
        forward_hours: int = 24,
        size_sol: Decimal = Decimal("1"),
        slip_pct: Decimal = Decimal("5"),
        fee_pct: Decimal = Decimal("1"),
        priority_sol: Decimal = Decimal("0"),
    ) -> Iterator[ReplayEntry]:
    ```
  - Causality contract (tested): every snapshot used for entry or path has `observed_at <= end` **and** the entry snapshot is the first with `ts > decision_ts`; decision points whose forward window extends past `end` are skipped, never truncated into fake labels.

- [ ] **Step 1: Write the failing tests**

```python
def test_entry_priced_at_next_snapshot_with_impact(session):
    # Decision at 12:00 (price 1.00), next snapshot 12:05 at 1.20.
    # Entry must be 1.20 * (1 + slip) — not 1.00, not 1.20 flat.
    ...
    assert entry.entry_ts == ts_1205
    assert entry.entry_price == pytest.approx(1.20 * 1.05, rel=1e-6)  # default slip 5%, fee on size not price


def test_replay_ignores_snapshots_observed_after_decision(session):
    # Insert a snapshot with ts inside the window but observed_at after the
    # replay end (vendor-restated). Replay output must be byte-identical
    # to the run without it.
    ...


def test_feature_timestamps_precede_label_window(session):
    # Leakage audit: for every entry, entry_ts > decision_ts and every
    # forward_rows ts is > decision_ts and <= decision_ts + forward window.
    for entry in iter_replay_entries(...):
        assert entry.entry_ts > entry.decision_ts
        assert all(entry.decision_ts < ts <= entry.decision_ts + timedelta(hours=24)
                   for ts, _ in entry.forward_rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_replay_harness.py -v`
Expected: FAIL — `backtest.replay` does not exist.

- [ ] **Step 3: Implement `backtest/replay.py`**

Grid decision times from `start` to `end` at `step_hours`; per asset, load rows once via `point_in_time_market_rows(..., decision_ts=end)` filtered to `observed_at <= end`, then walk the grid in memory (no per-step queries). Entry = first row with `ts > decision_ts`; skip when none, or when `entry_ts + forward_hours > end`. Impact via `simulate_buy` with the snapshot price as `quote_price` (Decimal(str(price))). Path via `forward_path` over rows in the window.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_replay_harness.py tests/test_backtest.py tests/test_backtest_consistency.py tests/test_leakage.py tests/test_leakage_audit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/replay.py tests/test_replay_harness.py
git commit -m "feat(backtest): causal replay harness with next-snapshot impact-priced entries and MFE paths"
```

---

### Task 5: Entry rank composite v1 + falsification run

Spec §3. The validated rank-sum trio from the Ticket 1 edge hunt (`unique_buyers_estimate`, `buy_sell_ratio`, `liquidity_depth` — see `scripts/forward_validate_ranksum.py::rank_sum_for_row`) gets two additions: a **net-inflow** feature (Δ quote reserves — `storage.models.LiquiditySnapshot.reserve_quote` exists but no feature reads it; the factory's liquidity code reads only `reserve_usd`) and a wash-trade-lite adjustment. "Curve velocity where available" = the reserve-quote growth rate itself on AMM pools (the bonding-curve/pool-reserve trajectory); where `reserve_quote` is null, fall back to `reserve_usd` growth and mark the component source in the feature's availability flag via the existing `_feature(...)` present/missing pattern in `features/factory.py`.

**Files:**
- Modify: `common/config.py` (Settings: `feature_net_inflow_lookback_hours: float = 1.0`, `entry_wash_volume_per_buyer: float = 20.0`, `entry_wash_downweight: float = 0.5`)
- Modify: `features/definitions.py` (`FEATURE_NAMES` + `FEATURE_LABELS`: append `"net_inflow_ratio"`)
- Modify: `features/factory.py` (`FeatureFactory._net_inflow(...)` beside `_holder_growth`, wired into `build_for_asset`)
- Create: `scoring/entry_rank.py`
- Create: `scripts/falsify_entry_rank.py` (entrypoint pattern per `tests/test_script_entrypoint_pattern.py`: `main()` + `if __name__ == "__main__"`)
- Test: `tests/test_net_inflow_feature.py` (create), `tests/test_entry_rank.py` (create)

**Interfaces:**
- Consumes: `FeatureFactory.build_for_asset(session, asset, decision_ts)` returning `FeatureValue`s with `.name`; `models.LiquiditySnapshot` (`pool_id`, `ts`, `observed_at`, `reserve_quote`, `reserve_usd`) joined via `models.Pool`/`models.Pair` as the factory's existing liquidity queries do; `backtest.replay.iter_replay_entries` + `ReplayEntry` (Task 4); `forecast.labels.ForwardPath` (Task 2).
- Produces:
  - Feature `net_inflow_ratio: float` = `(reserve_quote_now - reserve_quote_prev) / reserve_quote_prev` over `feature_net_inflow_lookback_hours`, point-in-time (`ts <= decision_ts`, `observed_at <= decision_ts`); `None` (missing) when no prior quote exists.
  - ```python
    # scoring/entry_rank.py
    ENTRY_RANK_COMPONENTS: tuple[str, ...] = (
        "net_inflow_ratio", "buy_sell_ratio", "unique_buyers_estimate", "liquidity_depth",
    )

    @dataclass(frozen=True)
    class EntryCandidate:
        asset_id: int
        rank_sum: float
        components: dict[str, float]   # percentile rank per component, 0..1
        wash_flagged: bool

    def wash_lite_adjust(
        features: dict[str, float], *, settings: Settings
    ) -> tuple[dict[str, float], bool]:
        """Down-weight buy_sell_ratio when volume-per-buyer screams wash.
        Inputs are existing FEATURE_NAMES only: intensity =
        volume_acceleration / max(unique_buyers_estimate, 1.0); when
        intensity > settings.entry_wash_volume_per_buyer, replace
        buy_sell_ratio with 1.0 + (buy_sell_ratio - 1.0) *
        settings.entry_wash_downweight and flag. (Same-tx round-trip
        exclusion needs swap-level data the lake does not persist
        per-snapshot; the flag is the honest v1.)"""

    def rank_entry_cohort(
        rows: list[tuple[int, dict[str, float]]], *, settings: Settings
    ) -> list[EntryCandidate]:
        """Percentile-rank each component within the cohort, sum, sort desc.
        Missing component = percentile 0.0 (absence is not neutral)."""
    ```
  - `scripts/falsify_entry_rank.py`: replays the last 14 fully-observed days via `iter_replay_entries`, ranks each day's cohort with `rank_entry_cohort` and with a volume-only baseline ranking, and writes `reports/entry-rank-falsification-<YYYYMMDD>.json` + `.md`. Headline: precision@top-20/day for `MFE_6h >= 2x` (share of the day's top 20 whose `forward_rows` reach `2 * entry_price` within 6h of entry). Exit code 0 iff composite precision ≥ 1.5× volume-ranking precision, else 1 (the report is written either way).

- [ ] **Step 1: Write the failing tests**

```python
def test_net_inflow_ratio_from_quote_reserves(session):
    # reserve_quote 100 -> 150 over the lookback: ratio +0.5, point-in-time
    # (a later reserve print must not change the value at decision_ts).
    ...
    assert feats["net_inflow_ratio"].value == pytest.approx(0.5)


def test_wash_lite_flags_and_downweights():
    # intensity = 400 / 10 = 40 > 20 threshold; buy_sell_ratio 3.0 ->
    # 1.0 + (3.0 - 1.0) * 0.5 = 2.0.
    feats = {"buy_sell_ratio": 3.0, "unique_buyers_estimate": 10.0, "volume_acceleration": 400.0}
    adjusted, flagged = wash_lite_adjust(feats, settings=get_settings())
    assert flagged is True
    assert adjusted["buy_sell_ratio"] == pytest.approx(2.0)


def test_rank_entry_cohort_missing_component_scores_zero():
    ...
    assert candidates[0].components["net_inflow_ratio"] == 0.0  # absent, not 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_net_inflow_feature.py tests/test_entry_rank.py -v`
Expected: FAIL — feature and module do not exist.

- [ ] **Step 3: Implement**

Factory feature following the `_holder_growth` query shape; definitions entries; `scoring/entry_rank.py` (percentile ranks via sorted order statistics, no new deps); the falsification script on top of Task 4 replay (features at replay decision points via `backtest.runner.build_replay_features`, never persisted features — replay must stay causal even where the live feature table has gaps).

- [ ] **Step 4: Run tests, then the falsification run**

Run: `pytest tests/test_net_inflow_feature.py tests/test_entry_rank.py tests/test_velocity_features.py tests/test_feature_completeness.py -v`
Expected: PASS.
Run: `python scripts/falsify_entry_rank.py` against a local DB copy with ≥14 days of snapshots.
Expected: report written to `reports/`; exit 0 (≥1.5× lift) or 1 — either outcome is recorded in the report and in the commit message; a failed falsification does **not** block the composite (it is a research instrument) but must be quoted verbatim in `STATE.md` by the implementer before committing. Three-window discipline (Global Constraints; spec §2 principle 7) binds this task: the component set was screened on window A and any weight choice is made on window B — the 14 replay days here are the evaluation window C and must be disjoint from both, with Benjamini–Hochberg correction quoted across the screened components in the report.

- [ ] **Step 5: Commit**

```bash
git add common/config.py features/definitions.py features/factory.py scoring/entry_rank.py scripts/falsify_entry_rank.py tests/test_net_inflow_feature.py tests/test_entry_rank.py reports/entry-rank-falsification-*.json reports/entry-rank-falsification-*.md STATE.md
git commit -m "feat(entry): net-inflow feature + rank-sum entry composite v1 with falsification run"
```

---

### Task 6: Entry evaluator + `entry_validated` gate

Spec §6. The harness (`validation/harness.py::run_harness`) already produces `MetricCell`s (output/metric/regime/n/value/CI/baseline/verdict, `validation/report.py:20`) over an embargoed walk-forward split, and already loads forecasts (`ForecastRow` with `p_ignition_24h`, `expected_hours_to_peak`). What it has never scored is the entry mission: is `p_ignition` calibrated, does `research_priority`'s top decile actually capture forward gain on **live** tokens, and how much lead time does a signal give? This task adds those cells and the gate that reads them.

**Files:**
- Modify: `common/config.py` (Settings, in the forecast-layer block ~line 497: `forecast_max_staleness_hours: float = 6.0`)
- Modify: `validation/harness.py` (new loader + `evaluate_entry`, wired into `run_harness`)
- Modify: `validation/status.py` (`entry_validation_status` beside `scores_validation_status`)
- Test: `tests/test_entry_validation.py` (create)

**Interfaces:**
- Consumes: `MetricCell` / `ValidationReport` (`validation/report.py`); `evaluate_probabilities(probs, labels, *, k=10, output, regime, min_samples=30, seed=7) -> list[MetricCell]` (existing — reuse for the `p_ignition_24h` brier/calibration block against Task 2's ignition outcomes); `LIVE_PHASES` / `DEAD_PHASES` (existing, `validation/harness.py:64`); the split built inside `run_harness` (`EMBARGO_HOURS`/`PURGE_HOURS`/`EVAL_FRACTION`) — `evaluate_entry` receives already-split rows, it never re-splits.
- Produces:
  - ```python
    # validation/harness.py
    @dataclass
    class EntryRow:
        asset_id: int
        decision_ts: datetime
        research_priority: float | None
        p_ignition_24h: float | None
        max_gain_pct: float          # from forward_max_gain_pct label
        hours_to_peak: float | None  # from hours_to_peak label
        phase_at_score: str | None   # live-stratum membership via LIVE_PHASES

    def load_entry_rows(session: Session) -> list[EntryRow]: ...
    def evaluate_entry(
        rows: list[EntryRow], *, regime: str, min_samples: int = 30
    ) -> list[MetricCell]: ...
    ```
    Cells emitted (output, metric): `("p_ignition_24h", "brier")` + `("p_ignition_24h", "calibration_error")` via `evaluate_probabilities` against `max_gain_pct >= settings.forecast_ignition_threshold`; `("entry_priority", "precision_at_20")` and `("entry_priority", "gain_lift_top_decile")` — **gain lift is a hit-rate ratio** (spec §6): the share of the top `research_priority` decile reaching the §5 gain threshold (`max_gain_pct >= 1.0`, the binary head) ÷ the same share over all rows. It is *not* a mean-gain ratio: mean MFE is dominated by a handful of extreme runners and would certify a ranking whose typical top pick never doubles; the same two cells under `"entry_priority:live"` restricted to `phase_at_score in LIVE_PHASES`; `("entry_priority:live", "median_hours_to_peak")` over ignited rows (value = median realized `hours_to_peak`, baseline = median over all ignited rows, lower-is-better verdict via the existing `verdict()` helper).
    **Forecast-freshness precondition (spec §3/§10):** before any entry cell is computed, `evaluate_entry`'s caller checks the newest `forecasts.decision_ts` in the eval data. If `now - newest > settings.forecast_max_staleness_hours`, no entry cells are emitted from those rows; instead a single `("forecasts", "freshness")` cell reports `insufficient_data` with the newest timestamp in its context, and `entry_validation_status` returns `entry_validated: False` with `entry_validation_reason` = `"stale forecasts: newest forecast decision_ts <ts> is older than forecast_max_staleness_hours"` (the reason string begins with the words `stale forecasts`). Stale rows are never evaluated as if current — on the live host the forecast cycle has been silent since 2026-11-27 09:18 UTC, so this is the expected live state until that operational fault is fixed.
  - ```python
    # validation/status.py
    _ENTRY_REQUIRED_CELLS: tuple[tuple[str, str, str], ...] = (
        ("p_ignition_24h", "calibration_error", "p_ignition calibration"),
        ("entry_priority:live", "precision_at_20", "entry precision@20 (live tokens)"),
        ("entry_priority:live", "gain_lift_top_decile", "entry gain lift (live tokens)"),
    )
    _ENTRY_STRICT_BEAT_OUTPUTS: frozenset[str] = frozenset({"entry_priority:live"})

    def entry_validation_status(reports_dir: Path | None = None) -> dict[str, Any]:
        """Returns {"entry_validated": bool, "entry_validation_report": str | None,
        "entry_validation_reason": str} from the newest validation-*.json,
        mirroring scores_validation_status's report selection and problem
        formatting. Strict-beat outputs must hold verdict beats_baseline /
        better_than_baseline; other required cells must not be
        worse_than_baseline / insufficient_data / unknown / missing."""
    ```

- [ ] **Step 1: Write the failing tests**

```python
def test_entry_cells_respect_embargo_split(session):
    # Rows inside the 48h embargo before the eval cutoff appear in NEITHER
    # the reference nor the eval entry cells (n counts prove it).
    ...


def test_entry_gate_requires_live_strict_beat(tmp_path):
    # Report whose headline entry cells pass but entry_priority:live is
    # "indistinguishable" -> entry_validated is False with the live cell
    # named in the reason (mirror test_scores_validation_surface.py's
    # test_headline_pass_without_live_edge_yields_false).
    ...


def test_entry_gate_passes_when_live_cells_beat(tmp_path):
    ...
    assert status["entry_validated"] is True


def test_gain_lift_is_hit_rate_ratio_not_mean_ratio():
    # 100 rows: top decile (10 rows by research_priority) has 4 rows at
    # max_gain_pct >= 1.0 (hit rate 0.40); all 100 rows have 10 hits
    # (base hit rate 0.10) -> gain_lift_top_decile == 4.0. One top-decile
    # row carries max_gain_pct = 50.0: under the old mean-ratio
    # definition that single runner would dominate the metric; the
    # hit-rate value must be exactly 4.0 regardless of its magnitude.
    cells = evaluate_entry(rows, regime="live")
    lift = cell(cells, "entry_priority", "gain_lift_top_decile")
    assert lift.value == pytest.approx(4.0)


def test_stale_forecasts_fail_entry_gate_with_named_reason(session, tmp_path):
    # Newest forecasts.decision_ts is 48h old with
    # forecast_max_staleness_hours = 6: no entry cells are computed
    # from the stale rows, and the status reads False, reason starting
    # with "stale forecasts".
    ...
    status = entry_validation_status(reports_dir=tmp_path)
    assert status["entry_validated"] is False
    assert status["entry_validation_reason"].startswith("stale forecasts")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_entry_validation.py -v`
Expected: FAIL — `load_entry_rows` / `evaluate_entry` / `entry_validation_status` do not exist.

- [ ] **Step 3: Implement**

Loader joins `labels` (Task 2 types + `ignition`) to `scores` (`research_priority`) and `forecasts` (`p_ignition_24h`) on `(asset_id, ts = decision_ts)`, phase from `risk_outcomes.lifecycle_phase_at_score` where present (the `OutcomeRow` pattern). Wire into `run_harness`: entry rows are split with the **same** cutoff and 48h embargo the outcome rows already use (never a separate split), eval-side rows go to `evaluate_entry`, and the cells land in the same `ValidationReport.cells`. Freshness first: `run_harness` computes `max(forecasts.decision_ts)` over the loaded forecast rows before calling `evaluate_entry`; on staleness it emits only the `("forecasts", "freshness")` cell and skips entry evaluation entirely. Gain lift per the Interfaces definition (hit-rate ratio at the §5 threshold 1.0, both the all-rows and `:live` variants). Status function by structural copy of `scores_validation_status` with the entry constants; when the newest report carries the freshness cell as `insufficient_data`, the reason is the `stale forecasts` string from the Interfaces block, ahead of any per-cell problem list.

- [ ] **Step 4: Three-window discipline in the evaluator (spec §2, principle 7)**

The split `run_harness` already builds is named for what it is: the embargoed tail is window **C** (evaluation — the only window `entry_validated` reads), the reference side is window **B** (baselines and any weight fitting), and any feature screening (Tasks 5, 13, 14) happens on window **A**, strictly before B. Implement as: `evaluate_entry` accepts only window-C rows (assert the embargo in the existing split test's terms); screening outputs from Tasks 5/13/14 enter the harness only as frozen component lists, never as numbers fitted on C; where a task screens more than one feature, its report applies Benjamini–Hochberg across the screened set and quotes corrected q-values (the screening scripts own the correction; the harness records the q-value source in the report context). Add a test: a row from window B passed to `evaluate_entry` raises / is excluded by construction of the split (extend `test_entry_cells_respect_embargo_split`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_entry_validation.py tests/test_validation_harness.py tests/test_scores_validation_surface.py tests/test_validation_cli.py -v`
Expected: PASS; pre-existing collapse cells and `scores_validated` behavior unchanged.

- [ ] **Step 6: Commit**

```bash
git add common/config.py validation/harness.py validation/status.py tests/test_entry_validation.py
git commit -m "feat(validation): entry evaluator cells + entry_validated gate (live-stratum strict beat, forecast-freshness precondition)"
```

---

### Task 7: Harvest labels (executable peak, harvest window, second leg)

Spec §4, §5. A price peak you cannot sell into is not a peak. Harvest labels describe the **executable** episode: the best gain actually reachable when each candidate sell is sized at a reference fraction of the episode's **peak** quote depth, when the harvest window opened, whether the episode then confirmed collapse, and whether it ran a second leg. Depth paths come from `models.LiquiditySnapshot` (`reserve_quote`, `reserve_usd`, `ts`, `observed_at`) joined through pools/pairs exactly as the label engines join `market_snapshots`.

**Files:**
- Modify: `common/config.py` (Settings: `harvest_theta: float = 0.125`, `harvest_collapse_drawdown_fraction: float = 0.50` — the share of the **peak price** given back before collapse is confirmed, `harvest_collapse_recovery_fraction: float = 0.20` — a recovery to within this fraction of the peak inside 4h negates a confirmation, `harvest_second_leg_multiple: float = 1.5`, `harvest_second_leg_drawdown_fraction: float = 0.30`)
- Create: `forecast/harvest.py`
- Modify: `forecast/labels.py` (constants + wiring in `LabelEngine.generate` and `seed_labels_at_feature_timestamps`)
- Test: `tests/test_harvest_labels.py` (create)

**Interfaces:**
- Consumes: `forecast.labels.ForwardPath` / `forward_path` / `upsert_label` / `forward_evidence_sufficient` (Tasks 2); `models.LiquiditySnapshot` depth rows aligned to the price path by ts (last depth at or before each price ts, point-in-time); reference size set is the constant below (evaluator sensitivity set — labels are written at all three sizes so Task 8 never recomputes paths).
- Produces:
  - Label constants in `forecast/labels.py`: `LABEL_EXECUTABLE_PEAK_GAIN_S02 = "executable_peak_gain_s02"`, `LABEL_EXECUTABLE_PEAK_GAIN_S10 = "executable_peak_gain_s10"`, `LABEL_EXECUTABLE_PEAK_GAIN_S25 = "executable_peak_gain_s25"`, `LABEL_HARVEST_WINDOW_START = "harvest_window_start_hours"`, `LABEL_COLLAPSE_CONFIRMED = "collapse_confirmed"`, `LABEL_SECOND_LEG = "second_leg"` (all present in Task 1's `MISSION_LABEL_TYPES`).
  - ```python
    # forecast/harvest.py
    REFERENCE_DEPTH_FRACTIONS: tuple[float, ...] = (0.02, 0.10, 0.25)

    @dataclass(frozen=True)
    class HarvestPath:
        executable_peak_gain_pct: dict[float, float]  # keyed by reference fraction
        harvest_window_start_hours: float | None      # start of the first maximal harvest-window interval (pinned semantics below), hours from decision; None when no row qualifies
        collapse_confirmed: bool
        second_leg: bool

    def harvest_path(
        entry_price: float,
        start: datetime,
        rows: list[tuple[datetime, float, float]],  # (ts, price, quote_depth_usd)
        *,
        settings: Settings,
    ) -> HarvestPath | None:  # None when rows is empty
    ```
    Semantics (pinned, spec §4/§5 verbatim): executable value at row `i` for reference fraction `f` uses the reference notional `N(f) = f * D_peak`, where `D_peak` is the episode's **peak** quote depth (USD) — reference sizes are fractions of peak depth, not entry depth. Selling `N(f)` into row `i`'s constant-product pool realizes the average fill price `exec_price_f[i] = price[i] * D[i] / (D[i] + N(f))`, with `D[i]` the quote depth (USD) at row `i`; `EV_f[i]` is that exec price (proceeds for the fixed reference notional are proportional to it). `executable_peak_gain_pct[f] = max_i(exec_price_f[i]) / entry_price - 1`, and `EV_peak(f) = max_i EV_f[i]`. Larger `f` → larger notional → deeper haircut, so the s02 ≥ s10 ≥ s25 ordering must hold on any path (Step 1's test asserts it).
    **Harvest window** (spec §5): the first maximal interval of consecutive rows where **both** `EV_f[i] >= (1 - theta) * EV_peak(f)` **and** `D[i] >= 0.50 * D_peak`. The window is computed at the evaluator's reference fraction `f = 0.10` (the same size Task 8 grades against); `harvest_window_start_hours` is the first row of that interval, in hours from the decision. A row failing the depth condition breaks the interval even when its EV qualifies — the back half of a price plateau with the pool drained is not a harvest window.
    **Collapse-confirmation** (spec §5): the first time price has drawn down ≥ `harvest_collapse_drawdown_fraction` (0.50) **from the episode's peak price**, with **no recovery to within `harvest_collapse_recovery_fraction` (0.20) of the peak** within the following 4h. A crossing that recovers inside 4h does not confirm; a later crossing that does not recover does. `collapse_confirmed` is True iff such a confirmed crossing exists in the window.
    **Second leg** (spec §5): after a ≥ `harvest_second_leg_drawdown_fraction` (0.30) drawdown from the prior EV peak, a new EV peak (at `f = 0.10`) exceeding `harvest_second_leg_multiple` (1.5×) the prior EV peak. The episode stays one episode; the flag is kept, never deleted or split away.
  - Wiring: both label engines write the six harvest label types beside the forward labels when a depth path exists for the asset; counts dicts gain `"harvest"` (rows written). `collapse_confirmed` / `second_leg` are binary `"1"`/`"0"`; the rest continuous `f"{value:.6f}"`. No depth path → no harvest labels for that point (absence, not zeros).

- [ ] **Step 1: Write the failing tests**

The executable-peak formula is defined by this test (average-price fill against a constant-product pool: selling `q` quote-worth into depth `D` realizes `price * D / (D + q)`):

```python
def test_executable_peak_haircuts_thin_peak():
    # Rows (price, quote depth USD), entry price 1.00:
    #   +0h (1.00, 2_000)   +1h (2.00, 10_000)
    #   +2h (3.00, 1_000)   +3h (2.20, 3_000)
    # Peak depth D_peak = 10_000 (at +1h), so the reference notionals
    # are fractions of PEAK depth: N(0.02)=200, N(0.10)=1_000,
    # N(0.25)=2_500. Exec prices price*D/(D+N) per row:
    #   f=0.02: 0.909 / 1.961 / 2.500 / 2.063 -> peak 2.500, gain 1.50
    #   f=0.10: 0.667 / 1.818 / 1.500 / 1.650 -> peak 1.818, gain 0.818
    #     (the 10% size's executable peak is at +1h, BEFORE the price
    #     peak — the depth was there, not at the thin 3.00 print)
    #   f=0.25: 0.444 / 1.600 / 0.857 / 1.200 -> peak 1.600, gain 0.60
    # Gains strictly decrease with reference size.
    ...
    path = harvest_path(1.00, start, rows, settings=get_settings())
    assert path.executable_peak_gain_pct[0.02] == pytest.approx(1.50)
    assert path.executable_peak_gain_pct[0.10] == pytest.approx(0.818, abs=0.001)
    assert path.executable_peak_gain_pct[0.25] == pytest.approx(0.60)


def test_harvest_window_opens_at_theta_below_peak():
    # theta = 0.125, window at the f=0.10 reference. Rows:
    #   +0h (1.00, 8_000)   +1h (1.60, 9_000)   +2h (2.60, 10_000)
    #   +2.5h (2.95, 4_000) +3h (3.00, 6_000)   +4h (2.90, 2_000)
    # D_peak = 10_000, N(0.10) = 1_000. EV (exec price) per row:
    #   0.889 / 1.440 / 2.364 / 2.360 / 2.571 / 1.933; EV_peak = 2.571
    #   at +3h; (1 - theta) * EV_peak = 0.875 * 2.571 = 2.250.
    # EV condition holds at +2h, +2.5h, +3h. Depth >= 50% of D_peak
    # (5_000) holds at +2h (10_000) and +3h (6_000) but FAILS at +2.5h
    # (4_000) — so the first maximal interval is [+2h] alone, and the
    # window opens at +2h. On EV alone the window would run unbroken
    # +2h -> +3h; the depth condition is what splits it.
    ...
    assert path.harvest_window_start_hours == pytest.approx(2.0)


def test_collapse_confirmation_requires_half_drawdown_from_peak():
    # Peak price 3.00 (entry 1.00). Confirmation = first drawdown of
    # >= 50% FROM PEAK (price <= 1.50) with no recovery to within 20%
    # of peak (>= 2.40) within the following 4h.
    # Fixture: +5h 1.40 (crossing #1), +7h 2.50 (recovery inside 4h ->
    # crossing #1 negated), +9h 1.30 (crossing #2), +10..13h all
    # <= 2.40 -> crossing #2 confirms.
    ...
    assert path.collapse_confirmed is True


def test_collapse_not_confirmed_when_recovery_within_4h():
    # Only crossing #1 from the fixture above (dip to 1.40, back to
    # 2.50 two hours later, never re-crosses): not confirmed. A dip to
    # 1.60 (46.7% drawdown) never crosses at all.
    ...
    assert path.collapse_confirmed is False


def test_second_leg_episode_retained_and_flagged():
    # Depth constant 10_000 (EV ratios track price ratios). Entry 1.00,
    # first EV peak at price 3.00 (+3h), drawdown to 2.00 at +6h
    # (-33.3% >= the 30% second-leg drawdown; above the 1.50 collapse
    # line, so no collapse crossing), then a new peak at 4.60 (+10h):
    # 4.60 / 3.00 = 1.533 > 1.5x the prior EV peak. ONE episode:
    # second_leg == "1", collapse_confirmed == "0", and all six
    # harvest labels present.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_harvest_labels.py -v`
Expected: FAIL — `forecast/harvest.py` does not exist.

- [ ] **Step 3: Implement with the pinned formula**

In `forecast/harvest.py`, implement exactly the Interfaces formula: `N(f) = f * D_peak` (peak quote depth over the episode's rows); `exec_price_f[i] = price[i] * D[i] / (D[i] + N(f))`; executable peak per fraction is the max over rows. (Depth rows with `quote_depth_usd is None` or `<= 0` are skipped for executability but still count for the price peak/window logic; `D_peak` is the max over rows with a usable depth.) Window, collapse-confirmation, and second-leg per the pinned semantics in the Interfaces block, thresholds from `settings.harvest_theta` / `settings.harvest_collapse_drawdown_fraction` / `settings.harvest_collapse_recovery_fraction` / `settings.harvest_second_leg_multiple` / `settings.harvest_second_leg_drawdown_fraction`. Wire into both engines in `forecast/labels.py` (this task extends `test_mission_label_types_match_label_constants`'s defined set with the six harvest constants).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_harvest_labels.py tests/test_forward_labels.py tests/test_features_retention.py tests/test_label_evidence_density.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add common/config.py forecast/harvest.py forecast/labels.py tests/test_harvest_labels.py tests/test_features_retention.py
git commit -m "feat(labels): harvest labels — executable peak at 2/10/25% depth, window, collapse-confirm, second-leg"
```

---

### Task 8: Exit evaluator + `exit_validated` gate

Spec §4, §6. The exit mission is graded on **captured fraction**: of the gain that was actually executable at the peak (PEV = perfect executable value, Task 7's 10%-reference executable peak), how much did the exit policy keep? Regret decomposes into the executability gap (clairvoyant price peak vs PEV), exit-timing regret (PEV vs policy gross), and cost drag (policy gross vs policy net). Benchmarks: a fixed ladder, a fixed 24h horizon exit, and the clairvoyant price peak.

**Files:**
- Modify: `validation/harness.py` (`ExitRow`, `captured_fraction`, `regret_decomposition`, `evaluate_exit`)
- Modify: `validation/status.py` (`exit_validation_status`)
- Test: `tests/test_exit_validation.py` (create; synthetic episodes only — no DB fixtures needed beyond the harness's existing patterns)

**Interfaces:**
- Consumes: `MetricCell`, `verdict()` (as used by `evaluate_probabilities`' cell builder), Task 7 label semantics for PEV (`executable_peak_gain_s10`).
- Produces:
  - ```python
    # validation/harness.py
    @dataclass
    class ExitRow:
        asset_id: int
        decision_ts: datetime
        pev_gain_pct: float            # executable peak gain (10% reference)
        clairvoyant_gain_pct: float    # raw price-peak gain (unexecutable bound)
        policy_gain_pct: float         # net of costs, the policy under test
        ladder_gain_pct: float         # fixed-ladder benchmark, net
        horizon_gain_pct: float        # sell-all at entry + forward window, net

    def captured_fraction(realized_gain_pct: float, pev_gain_pct: float) -> float:
        """realized / pev; pev <= 0 -> 0.0 when realized <= 0 else 1.0 (no
        executable upside existed; keeping anything is full capture)."""

    def regret_decomposition(row: ExitRow) -> dict[str, float]:
        """{"executability_gap": clairvoyant - pev,
            "exit_timing_regret": pev - policy_gross_part,
            "total_regret": clairvoyant - policy} — policy_gain_pct is net,
            so cost_drag = total - executability_gap - exit_timing_regret is
            reported as the residual key "cost_drag"."""

    def evaluate_exit(
        rows: list[ExitRow], *, regime: str, min_samples: int = 30
    ) -> list[MetricCell]: ...
    ```
    Cells: `("exit_policy", "captured_fraction")` — value = mean captured fraction of `policy_gain_pct` vs PEV, baseline = mean captured fraction of `ladder_gain_pct` vs PEV, verdict via `verdict()` (higher is better); `("exit_policy", "median_captured_fraction")` — value = **median** captured fraction of the policy vs PEV, baseline = median captured fraction of the ladder vs PEV; `("exit_policy", "p25_captured_fraction")` — value = **25th-percentile** captured fraction of the policy vs PEV, baseline = the ladder's p25 (spec §6: the gate reads median and p25, because a policy that helps typical trades and hurts bad ones fails where the money is lost); `("exit_policy", "mean_total_regret")`; `("exit_policy", "horizon_captured_fraction")` (same construction for the horizon benchmark, informational).
  - ```python
    # validation/status.py
    _EXIT_REQUIRED_CELLS: tuple[tuple[str, str, str], ...] = (
        ("exit_policy", "captured_fraction", "exit captured fraction vs ladder"),
        ("exit_policy", "median_captured_fraction", "exit median captured fraction vs ladder"),
        ("exit_policy", "p25_captured_fraction", "exit p25 captured fraction vs ladder"),
    )
    _EXIT_STRICT_BEAT_OUTPUTS: frozenset[str] = frozenset({"exit_policy"})

    def exit_validation_status(reports_dir: Path | None = None) -> dict[str, Any]:
        """{"exit_validated": bool, "exit_validation_report": ..., "exit_validation_reason": ...};
        the captured-fraction cell must strict-beat the ladder baseline, and
        BOTH the median and the p25 captured-fraction cells must strict-beat
        their ladder baselines — a mean-only win does not open the gate."""
    ```
  - `run_harness` wiring is **deferred to Task 9** (real `ExitRow`s need the policy); this task's evaluator is exercised on synthetic rows and via a `load_exit_rows` stub that returns `[]` (cells then report `insufficient_data`, gate reads False — the honest state until Task 9).

- [ ] **Step 1: Write the failing tests**

```python
def test_captured_fraction_edges():
    assert captured_fraction(1.0, 2.0) == pytest.approx(0.5)
    assert captured_fraction(0.0, 0.0) == 0.0
    assert captured_fraction(0.3, -0.1) == 1.0


def test_regret_decomposition_sums_to_total():
    row = ExitRow(asset_id=1, decision_ts=TS, pev_gain_pct=1.8,
                  clairvoyant_gain_pct=2.0, policy_gain_pct=1.2,
                  ladder_gain_pct=1.0, horizon_gain_pct=0.4)
    parts = regret_decomposition(row)
    assert parts["executability_gap"] == pytest.approx(0.2)
    assert sum(parts.values()) == pytest.approx(2.0 - 1.2)


def test_exit_gate_requires_strict_beat_over_ladder(tmp_path):
    # captured_fraction cell "indistinguishable" -> exit_validated False.
    ...


def test_exit_gate_fails_when_median_beats_but_p25_does_not(tmp_path):
    # Mean and median captured-fraction cells beat the ladder, but the
    # p25 cell is "indistinguishable" -> exit_validated False, with the
    # p25 cell named in the reason. Spec strength: median AND p25.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_exit_validation.py -v`
Expected: FAIL — none of the names exist.

- [ ] **Step 3: Implement**

Pure functions + cell builder mirroring `evaluate_probabilities`' CI approach (asset-bootstrap via the existing `_bootstrap_ci` import in the harness). Status function by structural copy of `entry_validation_status` (Task 6).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exit_validation.py tests/test_entry_validation.py tests/test_validation_harness.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add validation/harness.py validation/status.py tests/test_exit_validation.py
git commit -m "feat(validation): exit evaluator (captured fraction vs PEV, regret decomposition) + exit_validated gate"
```

---

### Task 9: EV-trailing exit + flow-armed ladder (rule-based v1)

Spec §4. The policy that Task 8 grades. Rules, in priority order: (1) hard override — an LP-removal event exits everything now; (2) ladder floor — fixed gain rungs sell fixed fractions no matter what (the benchmark-grade behavior, guaranteed capture); (3) once **armed** by flow rollover (net-flow ratio crossing down through its threshold) **and** buyer stall (unique-buyer growth ≤ 0), the remaining position exits in depth-paced tranches (each ≤ 10–15% of current quote depth); (4) the runner that survives arming trails the peak by the EV-trail fraction and exits when price gives back that fraction from peak. Sizing uses `models.LiquiditySnapshot` depth at decision time; flow inputs reuse Task 5's `net_inflow_ratio` semantics.

**Files:**
- Modify: `common/config.py` (Settings: `exit_ladder_rungs: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)`, `exit_ladder_fraction: float = 0.25`, `exit_max_depth_fraction: float = 0.125`, `exit_trail_fraction: float = 0.15`, `exit_nfr_arm_threshold: float = 0.0`, `exit_pacing_enabled: bool = True` — default governed by the Task 15 gate in Step 5)
- Create: `trading/exit_policy.py`
- Modify: `validation/harness.py` (real `load_exit_rows` replacing Task 8's stub; `run_harness` emits exit cells)
- Test: `tests/test_exit_policy.py` (create)

**Interfaces:**
- Consumes: `backtest.replay.ReplayEntry` / `iter_replay_entries` (Task 4); `validation.harness.ExitRow` (Task 8); `models.LiquidityRemovalEvent` (exists — the hard-signal source); `trading.costs.simulate_sell(*, qty, quote_price, slip_pct, fee_pct, priority_sol, price_native=None) -> tuple[Decimal, Fill]` for net proceeds.
- Produces:
  - ```python
    # trading/exit_policy.py
    @dataclass
    class PositionState:
        entry_price: float
        qty_remaining: float          # fraction of original position, 1.0 -> 0.0
        peak_price: float
        rungs_hit: int = 0
        armed: bool = False
        exited: bool = False

    @dataclass(frozen=True)
    class ExitSnapshot:
        ts: datetime
        price: float
        quote_depth_usd: float
        net_flow_ratio: float         # Task 5 net-inflow semantics, latest window
        unique_buyers_delta: float    # change since previous snapshot
        lp_removed: bool = False

    @dataclass(frozen=True)
    class ExitDecision:
        action: str                   # "hold" | "sell_tranche" | "exit_all"
        fraction: float               # of qty_remaining to sell now
        reason: str                   # "lp_removal" | "ladder_rung" | "armed_tranche" | "ev_trail" | "hold"

    class ExitPolicy:
        def __init__(self, settings: Settings) -> None: ...
        def on_snapshot(self, state: PositionState, snap: ExitSnapshot) -> ExitDecision: ...
    ```
    Pinned behavior: tranche size when armed = the largest fraction of `qty_remaining` whose quote value ≤ `exit_max_depth_fraction × snap.quote_depth_usd` (position valued at `snap.price`); ladder rung `i` fires once when `snap.price >= entry_price * (1 + exit_ladder_rungs[i])`, selling `exit_ladder_fraction` of the **original** position; EV-trail active only when `armed` or all rungs hit: `snap.price <= state.peak_price * (1 - exit_trail_fraction)` → `exit_all`; arming requires both conditions on the same snapshot and never disarms.
  - `trading.exit_policy.replay_exit_rows(session, *, start, end, settings) -> list[ExitRow]` — runs the policy and the fixed-ladder/horizon/clairvoyant benchmarks over Task 4 replay entries (depth path from `LiquiditySnapshot`; `lp_removed` from `LiquidityRemovalEvent` in-window), netting costs via `simulate_sell`. `validation.harness.load_exit_rows(session, *, settings)` delegates to it; `run_harness` adds `evaluate_exit(...)` cells.

- [ ] **Step 1: Write the failing tests**

```python
def test_lp_removal_exits_all_immediately(policy, state):
    decision = policy.on_snapshot(state, snap(price=1.1, lp_removed=True))
    assert (decision.action, decision.reason) == ("exit_all", "lp_removal")


def test_ladder_rung_sells_quarter_of_original(policy, state):
    decision = policy.on_snapshot(state, snap(price=1.6))  # +60% crosses rung 0.5
    assert decision.reason == "ladder_rung"
    assert decision.fraction == pytest.approx(0.25)


def test_no_arming_on_flow_rollover_alone(policy, state):
    # NFR negative but buyers still growing: not armed, no tranche.
    decision = policy.on_snapshot(state, snap(net_flow_ratio=-0.2, unique_buyers_delta=5.0))
    assert decision.action == "hold"


def test_armed_tranche_respects_depth_cap(policy, state):
    # Position quote value 10_000, depth 8_000: one tranche sells at most
    # 0.125 * 8_000 = 1_000 quote value = 10% of the position.
    ...
    assert decision.fraction <= 0.10 + 1e-9


def test_ev_trail_exits_after_peak_giveback(policy, state):
    # Armed; peak 4.00, price 3.30 (giveback 17.5% > trail 15%): exit_all.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_exit_policy.py -v`
Expected: FAIL — `trading/exit_policy.py` does not exist.

- [ ] **Step 3: Implement**

Policy as a pure state machine (no DB access inside `on_snapshot`); depth/flow assembly lives in `replay_exit_rows`. Wire `load_exit_rows` + `run_harness`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exit_policy.py tests/test_exit_validation.py tests/test_validation_harness.py tests/test_replay_harness.py -v`
Expected: PASS.

- [ ] **Step 5: Acceptance gate — median AND p25 strict beat on held-out synthetic episodes**

Before this policy may be committed, score it against the fixed-ladder benchmark on a held-out set of synthetic episodes (episode generator in `tests/test_exit_policy.py`, seeds disjoint from every fixture used in Steps 1–4): build `ExitRow`s via `replay_exit_rows` over the synthetic episodes and run `evaluate_exit`. The `("exit_policy", "median_captured_fraction")` and `("exit_policy", "p25_captured_fraction")` cells must **both** hold strict-beat verdicts (`beats_baseline` / `better_than_baseline`) against the ladder. If either fails, the policy does not commit as-is: tune only the `exit_*` Settings constants, record the failed cell values verbatim in `STATE.md`, and re-run this step — do not weaken the cells, and do not ship the policy behind the mean captured-fraction cell alone.

**Dependency note (Task 15):** the armed-tranche pacing layer (depth-capped tranche sizing) may not be enabled by default unless the Task 15 depth-lead study reports median depth lead > 0. If the study has not run, or its median lead is ≤ 0, `exit_pacing_enabled` (add the Settings field in this task, default True) is committed with default **False** and the tranches fall back to the ladder's fixed fractions until the study clears it.

- [ ] **Step 6: Commit**

```bash
git add common/config.py trading/exit_policy.py validation/harness.py tests/test_exit_policy.py
git commit -m "feat(exit): EV-trailing flow-armed ladder policy v1, replay-scored by the exit evaluator"
```

---

### Task 10: `research_priority` reframe — entry ranking is an entry ranking

Spec §3, §8. Today `scoring/formulas.py::compute_scores` computes `research_priority = clamp(hype * confidence/100 * liquidity_access/100 - risk*0.35 - manipulation*0.20 - uncertainty*0.10 - 0.15*clamp(collapse_probability*100) + 0.05*prelaunch)` and then **zeroes it entirely for BLACK-band assets** (line ~311). That is collapse-avoidance wearing an entry ranking's clothes: the coin most likely to moon-and-die — the mission's exact target — is ranked last. This task makes `research_priority` an entry score driven by ignition probability, expected gain, and hours-to-peak; collapse probability leaves the entry ranking and remains an input to position management (`exit_risk`, which keeps its existing formula including its collapse term).

**Files:**
- Modify: `scoring/formulas.py` (`compute_scores`, `research_priority` block ~lines 303–312; the band-floor block ~lines 89–107 stays untouched)
- Test: `tests/test_research_priority_reframe.py` (create)

**Interfaces:**
- Consumes: existing feature dict keys read by `compute_scores` — `collapse_probability_24h` (0..1 scale as used at line ~308) plus the forecast-derived inputs already flowing through `features`: ignition probability is available as the `ignition_signal` feature today; where the persisted forecast exists, `scoring/engine.py` passes `p_ignition_24h` / `expected_hours_to_peak` through the same feature dict under those exact keys when present (verify in `scoring/engine.py` before writing code; if the keys are absent there, read them from `features.get("p_ignition_24h", ignition_signal)` with the fallback pinned in the test).
- Produces:
  - ```python
    # scoring/formulas.py — new research_priority semantics
    # (settings via get_settings(), as in forecast/labels.py)
    entry_score = clamp(
        0.45 * clamp(p_ignition * 100.0)
        + 0.30 * clamp(expected_gain_pct * 100.0)      # expected_gain_pct pinned in Step 3
        + 0.25 * clamp(100.0 - expected_hours_to_peak * (100.0 / get_settings().forecast_forward_hours))
    )
    research_priority = entry_score  # no band zeroing, no collapse subtraction
    ```
    Exact composition is pinned in Step 3 from the inputs that demonstrably exist; the **invariants** are the contract: (a) `research_priority` is monotonically non-decreasing in `p_ignition` holding the rest fixed; (b) it does not reference `collapse_probability_24h`, `risk_assessment.score`, or `final_band`; (c) `exit_risk`'s formula is byte-for-byte unchanged; (d) `ScoreResult.drivers` gains `"p_ignition_24h"` and `"expected_hours_to_peak"` entries so the surface can explain an entry rank.
  - Entry floor (the band-floor pattern, reused): if `p_ignition >= 0.5` and `research_priority` computes below the cohort's GREEN-equivalent floor used by `band_from_collapse_probability`'s inverse mapping in `risk_engine/rules.py`, raise it to that floor and append a `risk_reasons`-style note to `drivers["entry_floor"]`. If the implementer finds no clean inverse mapping, **drop the floor** and record the decision in the commit message — the invariants above are the deliverable, the floor is conditional per the spec ("if design requires").

- [ ] **Step 1: Write the failing tests**

```python
def test_high_collapse_high_ignition_coin_ranks_high():
    # The old formula zeroed this coin (BLACK band, collapse_probability .9).
    # Mission semantics: p_ignition .8 with a fast expected peak is a TOP
    # entry candidate; collapse risk is the exit policy's problem.
    result = compute_scores(features(p_ignition=0.8, collapse_probability=0.9,
                                     expected_hours_to_peak=3.0, band="BLACK"))
    assert result.research_priority >= 60.0


def test_research_priority_monotone_in_p_ignition():
    low = compute_scores(features(p_ignition=0.2, ...))
    high = compute_scores(features(p_ignition=0.7, ...))
    assert high.research_priority > low.research_priority


def test_exit_risk_formula_unchanged():
    # Golden-value test: exit_risk for a fixed feature dict equals the
    # pre-reframe value (compute it from the current code before editing).
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_research_priority_reframe.py -v`
Expected: FAIL — the BLACK/high-collapse case returns `research_priority == 0.0` (or near it) under the current formula.

- [ ] **Step 3: Implement**

First record the golden `exit_risk` value from the unmodified code into the test. Then replace the `research_priority` block: expected gain proxy = `p_ignition * settings.forecast_ignition_threshold`-scaled forward gain is too clever — pin it as `p_ignition * (1.0 / max(expected_hours_to_peak, 1.0))`-free simple form: use the composition in Interfaces with `expected_gain_pct := p_ignition` when no gain forecast exists (documented in a code comment: until the entry model predicts gain magnitude, ignition probability carries both terms, weighted once). Remove the BLACK zeroing and the collapse/risk/manipulation subtractions from `research_priority` only. Keep `hype`, `confidence`, and every other `ScoreResult` field's computation untouched.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_research_priority_reframe.py tests/test_risk_scoring.py tests/test_band_floor.py tests/test_score_distribution.py tests/test_scoring_path_consistency.py -v`
Expected: PASS. Any pre-existing test that pinned the old `research_priority` semantics is updated to the new semantics in this commit, with the change called out in the commit message (search first: `grep -rn research_priority tests/`).

- [ ] **Step 5: Commit**

```bash
git add scoring/formulas.py tests/test_research_priority_reframe.py
git commit -m "feat(scoring): research_priority is an entry ranking (ignition/gain/lead-time); collapse moves to position management"
```

---

### Task 11: `scores_validated` retirement + surface rewiring + mission language

Spec §6, §8. One flag becomes two honest ones. `validation/status.py::scores_validation_status` is replaced by a combined mission status; `/trading/status` and `/paper/candidates` (both spread `**scores_validation_status()` in `api/trading_routes.py`) carry the new keys; the Streamlit banner in `ui/trading_view.py` reports entry and exit validation separately. Entry-phase transitions alert like danger phases: `pump_physics/engine.py::_ALERT_PHASES` is `{COLLAPSE, RUGGED, DEAD}` and `_maybe_transition_alert` fires only for those — IGNITION and PARABOLIC transitions get the same one-shot, deduped alert treatment. Docs take the spec §8 inventory: README headline (line 15: "It separates hype from structural danger…") rewritten to the two-front mission, GREEN semantics redefined as "no exit signal yet" wherever band semantics are documented, `docs/blueprint.md`'s phase-transition framing promoted from aspiration to mission statement.

**Files:**
- Modify: `validation/status.py`
- Modify: `api/trading_routes.py` (all `**scores_validation_status()` spreads, lines ~189/273/294/340/356/381)
- Modify: `ui/trading_view.py`
- Modify: `pump_physics/engine.py` (`_ALERT_PHASES`, `_maybe_transition_alert`, `LifecycleEngine._emit` call path)
- Modify: `README.md`, `STATE.md`, `docs/blueprint.md`
- Test: `tests/test_mission_status_surface.py` (create), `tests/test_scores_validation_surface.py` (update to the new keys), `tests/test_engine_phases.py` / `tests/test_pump_physics.py` (extend for entry alerts)

**Interfaces:**
- Consumes: `entry_validation_status()` (Task 6), `exit_validation_status()` (Task 8) — exact return dicts as defined there.
- Produces:
  - ```python
    # validation/status.py
    def mission_validation_status(reports_dir: Path | None = None) -> dict[str, Any]:
        """Merge of entry_validation_status + exit_validation_status.
        The keys scores_validated / scores_validation_reason are REMOVED;
        consumers must read entry_validated / exit_validated and their
        *_validation_reason strings."""
    ```
    `scores_validation_status` is deleted (not deprecated — the spec retires it; `grep -rn scores_validated` across the repo must return hits only in historical `reports/` and `docs/updates-*` files after this task).
  - `pump_physics`: `_ALERT_PHASES` gains `LifecyclePhase.IGNITION` and `LifecyclePhase.PARABOLIC`; alert message for entry phases names the phase and the same evidence fields; dedupe/idempotency identical (`lifecycle:{event.id}` ref). Alert-type controls (`ops.alert_quality.alert_generation_allowed`) apply unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_trading_status_carries_two_flags_not_one(session, queue, tmp_path):
    # GET /trading/status payload has entry_validated + exit_validated,
    # and "scores_validated" not in payload.
    ...


def test_ui_banner_names_entry_and_exit_separately():
    # tests/test_mission_status_surface.py mirrors
    # test_scores_validation_surface.py::test_ui_trading_view_exists:
    # trading_view source references both flags' reasons.
    ...


def test_ignition_transition_fires_one_alert(session):
    # Drive LifecycleEngine.scan across a SEEDING -> IGNITION transition
    # twice; exactly one lifecycle_transition Alert exists for the event.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mission_status_surface.py -v`
Expected: FAIL — payload still carries `scores_validated`; no IGNITION alert fires.

- [ ] **Step 3: Implement**

Status merge + delete; route spreads switched to `mission_validation_status()`; banner shows two lines (entry, exit) with their reasons; `_ALERT_PHASES` extended with an entry-phase message branch in `_maybe_transition_alert`. Docs: README headline + "Current verified state" mission paragraph, STATE.md head entry for the refactor, blueprint promotion — language from spec §1/§8, no new claims (validation state quoted from the status functions' current output).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mission_status_surface.py tests/test_scores_validation_surface.py tests/test_engine_phases.py tests/test_pump_physics.py tests/test_ui_navigation.py tests/test_api.py -v` and `grep -rn "scores_validated" --include="*.py" .` returns nothing outside `reports/`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add validation/status.py api/trading_routes.py ui/trading_view.py pump_physics/engine.py README.md STATE.md docs/blueprint.md tests/test_mission_status_surface.py tests/test_scores_validation_surface.py tests/test_engine_phases.py tests/test_pump_physics.py
git commit -m "feat(surface): retire scores_validated for entry_validated/exit_validated; entry-phase alerts; mission language"
```

---

### Task 12: Smart-money convergence (gated) + cluster/bundle gate v1 — the long pole

Spec §3. Two candidate entry signals, both guilty until proven innocent. **12a** is a gate, not a feature: a case-control test of whether point-in-time profitable-wallet convergence predicts pumps. Only if it clears ≥3× does **12b** build the wallet-PnL table and the convergence feature. **12c** is independent: a launch-bundle gate (same-block bundled buys at launch are a supply-control tell) shipped as an entry-rank component flag. 12a and 12c can land in either order; 12b is blocked on 12a's verdict. Each sub-part is its own test cycle and its own commit.

**Files:**
- Create (12a): `scripts/case_control_smart_money.py`
- Create (12b, gated): `storage/migrations/versions/0044_wallet_pnl_point_in_time.py`, `forecast/wallet_pnl.py` (builder), feature wiring in `features/definitions.py` + `features/factory.py` (`smart_money_convergence`), component wiring in `scoring/entry_rank.py`
- Modify (12c): `scoring/entry_rank.py` (bundle flag), ingestion-side extraction only if launch-tx data already persists — check `models.IgnitionEvent` / raw evidence for launch transactions first
- Test: `tests/test_smart_money_gate.py` (create; covers 12a's statistic and, when built, 12b's point-in-time discipline), `tests/test_entry_rank.py` (extend for 12c)

**Interfaces:**
- Consumes: `models.Holder` (`asset_id`, `wallet_address`, `ts`, `source_id` — holder snapshots exist; per-wallet trade/PnL history does **not**, which is why 12b needs a table); Task 2 forward labels (`forward_max_gain_pct`, ignition threshold) as case definitions; `backtest.runner.point_in_time_market_rows` causality discipline.
- Produces:
  - 12a: `scripts/case_control_smart_money.py` — cases = asset-decision points with `forward_max_gain_pct >= 1.0` (≥2x) in the replay window; controls = matched points (same day, similar `liquidity_depth` decile) below the ignition threshold. "Convergence" proxy from existing data only: ≥ `k` distinct holder wallets appearing in `holders` for the asset within the 6h before decision whose addresses also appear as early holders of ≥2 prior cases (a point-in-time "repeat winner" set, built strictly from data observed before each decision). Output `reports/smart-money-case-control-<YYYYMMDD>.json/.md` with pump share in converged vs control groups; exit 0 iff converged pump share ≥ 3× control share with n ≥ 30 converged points.
  - 12b (only on 12a exit 0): table `wallet_pnl_point_in_time` (migration 0044; columns `wallet_address`, `asset_id`, `ts`, `observed_at`, `realized_pnl_quote`, `unrealized_pnl_quote`, `method_version`; unique `(wallet_address, asset_id, ts)`), builder `forecast/wallet_pnl.py::build_wallet_pnl(session, *, decision_ts) -> dict[str, int]` (point-in-time: only snapshots/trades observed by `decision_ts`), feature `smart_money_convergence` = share of current inflow attributable to wallets in the top PnL decile as of decision time, and `scoring/entry_rank.py::ENTRY_RANK_COMPONENTS` gains it as an optional component (missing = 0.0 percentile, per Task 5's rule).
  - 12c: `EntryCandidate` (Task 5) gains `bundle_flagged: bool`. Cohort feature dicts may carry `"launch_bundle_share"` (0..1, same-block bundled share of launch-window buys, extracted per Step 5); `rank_entry_cohort` sets `bundle_flagged = launch_bundle_share >= settings.entry_bundle_share_threshold` (`entry_bundle_share_threshold: float = 0.30`, new Settings field) and records `"launch_bundle_share"` in `components`. Where launch-tx data is absent for an asset, the key is absent, `bundle_flagged=False`, and the falsification report notes the coverage gap — the share is never inferred from aggregates.
  - **Threshold reconciliation (do not "fix" either number):** spec §3 item 4's **40%** governs the **funding-cluster share** — the share of early float held by wallets sharing a funding source — which is a different, heavier measure built in Task 17's timeframe and deferred until then. 12c's **0.30** governs the **same-block bundle share** — the share of launch-window buys executed in same-block bundles. They are different measures of different things; 0.30 stands for bundle share, 40% stands for funding-cluster share, and neither is a typo for the other.

- [ ] **Step 1 (12a): Write the failing test, then the gate script**

```python
def test_case_control_statistic_counts_only_prior_winners():
    # A wallet whose first case-win is AFTER the decision point must not
    # count as a "repeat winner" for that point (point-in-time or it's
    # leakage with extra steps).
    ...
```

Run: `pytest tests/test_smart_money_gate.py -v` → FAIL, then implement the script's statistic as an importable function `scripts/case_control_smart_money.py::convergence_stats(...)` (script stays thin), re-run → PASS, then run the script against a local DB copy and record the verdict verbatim in the report + `STATE.md`.

- [ ] **Step 2 (12a): Commit the gate, whatever the verdict**

```bash
git add scripts/case_control_smart_money.py tests/test_smart_money_gate.py reports/smart-money-case-control-*.json reports/smart-money-case-control-*.md STATE.md
git commit -m "research(smart-money): case-control gate for wallet convergence (verdict in report)"
```

If the gate fails (< 3× or n < 30): **stop Task 12 here** — 12b is cancelled, not deferred-silently; note the cancellation in `STATE.md` and in the commit. 12c proceeds independently.

- [ ] **Step 3 (12b, only if gated in): TDD the PnL table + feature**

Tests first (`tests/test_smart_money_gate.py`): PnL builder ignores trades observed after `decision_ts`; convergence feature is 0.0/missing for assets with no PnL rows; migration 0044 upgrades and downgrades cleanly (pattern: `tests/test_migration_0039.py`). Then migration + `forecast/wallet_pnl.py` + feature + rank-component wiring. Run: `pytest tests/test_smart_money_gate.py tests/test_entry_rank.py tests/test_schema.py -v` → PASS.

- [ ] **Step 4 (12b): Commit**

```bash
git add storage/models.py storage/migrations/versions/0044_wallet_pnl_point_in_time.py forecast/wallet_pnl.py features/definitions.py features/factory.py scoring/entry_rank.py tests/test_smart_money_gate.py
git commit -m "feat(entry): point-in-time wallet PnL + smart_money_convergence feature (case-control gated)"
```

- [ ] **Step 5 (12c): TDD the bundle gate**

Tests first in `tests/test_entry_rank.py`: a cohort row with launch bundle share 0.45 is `bundle_flagged=True` and its rank is computed with the flag visible (flagging is informational in v1 — it does not zero the rank; the falsification script from Task 5 is re-run with the flag recorded so its effect is measured, not assumed). Implement extraction from persisted launch transactions if present; if the implementer confirms no launch-tx source exists in the lake, implement the gate against `models.IgnitionEvent.details` launch metadata where present and record the coverage limit in `STATE.md` — do not build a new ingestion pipeline inside this task.

- [ ] **Step 6 (12c): Run tests + commit**

Run: `pytest tests/test_entry_rank.py tests/test_smart_money_gate.py -v`
Expected: PASS.

```bash
git add common/config.py scoring/entry_rank.py tests/test_entry_rank.py STATE.md
git commit -m "feat(entry): launch bundle-share gate flag v1"
```

---

### Task 13: Social-presence prior (spec §3, item 3)

Spec §3. Presence flags **as observed at first observation of the coin**, applied as a prior multiplier on the entry composite — never a standalone signal. The token info is already ingested: `ingestion/service.py::_ingest_dexscreener_profiles` pulls DexScreener token profiles and persists `website_url` / `github_url` onto `assets` via `upsert_asset(..., first_seen_at=observed_at)` using `ingestion/normalizers.py::extract_profile_links`, and `normalize_dexscreener_pair` already reads the pair payload's `info.websites` / `info.links` (`_link_of_type`). What is missing: `extract_profile_links` only extracts website/github — Telegram and X links present in the same payloads are dropped — and nothing turns presence into a point-in-time flag or a prior. The raw profile payloads are also stored verbatim via `store_raw_evidence`, so first-observation presence is reconstructible without a schema change.

**Files:**
- Modify: `ingestion/normalizers.py` (`extract_profile_links` extended to Telegram / X)
- Modify: `ingestion/service.py` (`_ingest_dexscreener_profiles` passes the extended links through)
- Modify: `scoring/entry_rank.py` (prior multiplier on `rank_entry_cohort` output)
- Modify: `common/config.py` (Settings: `entry_social_prior_multiplier: float = 1.25`)
- Create: `scripts/social_prior_falsification.py` (entrypoint pattern per `tests/test_script_entrypoint_pattern.py`)
- Test: `tests/test_social_prior.py` (create)

**Interfaces:**
- Consumes: `ingestion.normalizers.extract_profile_links(payload) -> tuple[str | None, str | None]` (existing signature — extended, see Produces); `models.Asset` (`website_url`, `github_url`, `first_seen_at`); raw-evidence profile payloads (`store_raw_evidence` rows for the dexscreener source); Task 2's `forward_max_gain_pct` label (the pump label: `>= 1.0`); `scoring.entry_rank.rank_entry_cohort` (Task 5).
- Produces:
  - `extract_profile_links(payload) -> ProfileLinks` — a small frozen dataclass `(website_url, github_url, telegram_url, x_url)`; the two existing call sites are updated in this task (the tuple return is internal to `ingestion/`).
  - ```python
    # scoring/entry_rank.py
    @dataclass(frozen=True)
    class SocialPresence:
        website: bool
        github: bool
        telegram: bool
        x: bool

    def social_presence_at_first_observation(
        session: Session, asset_id: int
    ) -> SocialPresence:
        """Flags from the asset's FIRST observation only: stored
        website/github URLs, plus Telegram/X links in the earliest
        raw-evidence profile at or before first_seen_at. Later profile
        updates never rewrite the flags (point-in-time)."""

    # rank_entry_cohort gains keyword-only presence: dict[int, SocialPresence] | None = None;
    # a candidate with telegram or website present gets
    # rank_sum *= settings.entry_social_prior_multiplier (applied after the
    # rank-sum, before sorting). Absent presence data = no multiplier.
    ```
  - `scripts/social_prior_falsification.py`: contingency table over historical launches — rows: Telegram-present at first observation vs not; columns: pump label (`forward_max_gain_pct >= 1.0`) vs not — reporting lift = P(pump | telegram) / P(pump | no telegram) with counts. Writes `reports/social-prior-falsification-<YYYYMMDD>.json` + `.md`; exit 0 iff lift ≥ 2.0 with n(telegram-present) ≥ 30. **Demotion rule:** if lift < 2×, the prior is demoted to a tiebreaker in the same commit — `entry_social_prior_multiplier` default becomes 1.0 and `rank_entry_cohort` uses presence only to break exact rank_sum ties; the report and `STATE.md` record which state shipped.

- [ ] **Step 1: Write the failing tests**

```python
def test_extract_profile_links_captures_telegram_and_x():
    payload = {"links": [
        {"label": "telegram", "url": "https://t.me/example"},
        {"label": "twitter", "url": "https://x.com/example"},
        {"label": "website", "url": "https://example.com"},
    ]}
    links = extract_profile_links(payload)
    assert links.telegram_url == "https://t.me/example"
    assert links.x_url == "https://x.com/example"
    assert links.website_url == "https://example.com"


def test_presence_flags_frozen_at_first_observation(session):
    # A Telegram link that appears only in a LATER profile update must
    # not set the flag: presence is what was true at first observation.
    ...
    assert social_presence_at_first_observation(session, asset_id).telegram is False


def test_prior_multiplier_applies_after_rank_sum():
    # Two otherwise-identical candidates; the Telegram-present one is
    # multiplied by entry_social_prior_multiplier and outranks.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_social_prior.py -v`
Expected: FAIL — `ProfileLinks` / telegram extraction / presence function do not exist.

- [ ] **Step 3: Implement**

Extend the normalizer + profile ingest; presence derivation (stored URLs first, earliest raw-evidence profile for Telegram/X); multiplier wiring in `rank_entry_cohort`. Three-window discipline binds this task (Global Constraints): presence flags are point-in-time by construction, and the falsification below is window C only — the multiplier value is a prior chosen on window B reasoning, not fitted on the falsification window.

- [ ] **Step 4: Run tests, then the falsification run**

Run: `pytest tests/test_social_prior.py tests/test_entry_rank.py tests/test_normalizers.py -v`
Expected: PASS.
Run: `python scripts/social_prior_falsification.py` against a local DB copy with Task 2 labels generated.
Expected: report written; exit 0 (lift ≥ 2×) keeps the multiplier, exit 1 applies the demotion rule in Step 3's terms before committing. Either outcome is quoted verbatim in `STATE.md`.

- [ ] **Step 5: Commit**

```bash
git add ingestion/normalizers.py ingestion/service.py scoring/entry_rank.py common/config.py scripts/social_prior_falsification.py tests/test_social_prior.py reports/social-prior-falsification-*.json reports/social-prior-falsification-*.md STATE.md
git commit -m "feat(entry): social-presence prior at first observation, contingency-falsified (Task 13)"
```

---

### Task 14: Buyer-acceleration feature (spec §3, item 2's missing half)

Spec §3. Task 5 shipped the rank composite with `unique_buyers_estimate` as a level; spec item 2's actual signal is its **second difference** — acceleration of cleaned unique buyers. `features/factory.py` computes `unique_buyers_estimate` from snapshot buys (~line 400) but no feature differentiates it. This task adds the acceleration feature, computed from the wash-adjusted buyer series: Task 5's wash-lite flag (`scoring/entry_rank.py::wash_lite_adjust`) is applied to the inputs first, so a wash spike reads as flagged-and-damped, not as acceleration.

**Files:**
- Modify: `features/definitions.py` (`FEATURE_NAMES` + `FEATURE_LABELS`: append `"unique_buyers_acceleration"`)
- Modify: `features/factory.py` (`_buyer_acceleration(...)` beside the `unique_buyers_estimate` computation, wired into `build_for_asset`)
- Modify: `scoring/entry_rank.py` (`ENTRY_RANK_COMPONENTS` gains `"unique_buyers_acceleration"` as an optional component — missing = percentile 0.0, per Task 5's rule; wash-flagged cohorts exclude the term rather than rank on it)
- Test: `tests/test_buyer_acceleration.py` (create)

**Interfaces:**
- Consumes: the factory's existing per-asset snapshot walk (the same point-in-time rows `unique_buyers_estimate` is derived from: `ts <= decision_ts`, `observed_at <= decision_ts`); `scoring.entry_rank.wash_lite_adjust` (Task 5) for the wash flag on the current feature dict.
- Produces:
  - Feature `unique_buyers_acceleration: float` = second difference of the buyer estimate over the factory's snapshot grid: with buyer estimates `b(t-2), b(t-1), b(t)` at the three most recent snapshot times ≤ decision, acceleration = `(b(t) - b(t-1)) - (b(t-1) - b(t-2))`, normalized per hour by the grid spacing; `None` (missing) with fewer than three points. Sign convention: positive = buyer growth speeding up.
  - In `rank_entry_cohort`: when the cohort row's wash flag is set (Task 5 semantics), the acceleration component is dropped for that row (percentile 0.0 and the drop recorded in `components` as `"unique_buyers_acceleration_wash_dropped": 1.0`) — wash infrastructure must not be able to buy an acceleration rank.

- [ ] **Step 1: Write the failing tests**

```python
def test_acceleration_zero_for_linear_buyer_growth(session):
    # Buyers 100, 150, 200 at hourly snapshots: first differences
    # +50, +50 -> acceleration 0.
    ...
    assert feats["unique_buyers_acceleration"].value == pytest.approx(0.0)


def test_acceleration_positive_for_convex_growth(session):
    # Buyers 100, 120, 200: differences +20, +80 -> acceleration +60/h.
    ...
    assert feats["unique_buyers_acceleration"].value == pytest.approx(60.0)


def test_wash_flagged_row_drops_acceleration_component():
    # Cohort row flagged by wash_lite_adjust with a huge acceleration
    # value: component percentile is 0.0 and the drop is recorded.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_buyer_acceleration.py -v`
Expected: FAIL — feature does not exist.

- [ ] **Step 3: Implement**

Factory computation following the existing feature idiom (`_feature(...)` present/missing pattern, same freshness/source plumbing as `unique_buyers_estimate`); definitions entries; rank-component wiring. Three-window discipline binds this task (Global Constraints): the feature is screened on window A only; its falsification is the Task 5 replay re-run on window C with and without the term (spec §3 item 2's ablation), quoted in the commit message.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_buyer_acceleration.py tests/test_entry_rank.py tests/test_feature_completeness.py tests/test_velocity_features.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add features/definitions.py features/factory.py scoring/entry_rank.py tests/test_buyer_acceleration.py
git commit -m "feat(features): unique-buyers acceleration (second difference, wash-flagged rows dropped) (Task 14)"
```

---

### Task 15: Depth-lead study (spec §4, build order 1)

Spec §4. The cheapest test in the exit programme, and it gates a premise: **does quote depth peak before the executable-value peak?** If depth peaks first, Task 9's pacing layer (depth-capped tranches, slowing as depth decays) is reading a leading indicator; if depth peaks with or after EV, pacing is steering by a mirror. This study measures the lead per historical episode and reports the median. It is a study script + report, not a policy change — its only code consequence is the Task 9 gate recorded below and in Task 9 Step 5.

**Files:**
- Create: `scripts/depth_lead_study.py` (entrypoint pattern per `tests/test_script_entrypoint_pattern.py`)
- Test: `tests/test_depth_lead_study.py` (create)

**Interfaces:**
- Consumes: Task 7's harvest machinery — `forecast.harvest.harvest_path` / episode rows of `(ts, price, quote_depth_usd)` built from `models.LiquiditySnapshot` exactly as Task 7's wiring builds them; episodes = assets with a Task 7 executable peak (s10 gain > 0) in the study window.
- Produces:
  - ```python
    # scripts/depth_lead_study.py (statistic importable, script thin)
    @dataclass(frozen=True)
    class EpisodeLead:
        asset_id: int
        depth_peak_ts: datetime
        ev_peak_ts: datetime        # EV at the f=0.10 reference (Task 7 semantics)
        lead_snapshots: int         # index(ev_peak) - index(depth_peak); > 0 = depth led
        lead_hours: float

    def episode_lead(rows: list[tuple[datetime, float, float]], *, settings: Settings) -> EpisodeLead | None: ...
    def median_lead(leads: list[EpisodeLead]) -> float: ...   # median of lead_snapshots
    ```
  - Report `reports/depth-lead-study-<YYYYMMDD>.json` + `.md`: per-episode leads, median lead in snapshots and hours, share of episodes with lead > 0, n episodes. Exit 0 iff median lead > 0, else 1 (report written either way).
  - **GATE (binding on Task 9):** Task 9's pacing layer may not be enabled by default unless this study's median lead > 0. Task 9 Step 5 already carries the dependency: if the study has not run or its median lead ≤ 0, `exit_pacing_enabled` ships default **False**.

- [ ] **Step 1: Write the failing tests**

```python
def test_episode_lead_positive_when_depth_peaks_first():
    # Depth peaks at row 2, EV (f=0.10) peaks at row 5 -> lead +3.
    ...
    assert lead.lead_snapshots == 3


def test_episode_lead_negative_when_depth_trails_ev():
    # Depth still rising after the EV peak -> lead < 0.
    ...


def test_median_lead_uses_episode_median_not_mean():
    # One outlier episode with lead +40 must not carry the median.
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_depth_lead_study.py -v`
Expected: FAIL — the study module does not exist.

- [ ] **Step 3: Implement**

Statistic in the script module on top of Task 7's row builder (no new path code); thin `main()` that walks the study window's episodes, computes leads, writes the report.

- [ ] **Step 4: Run tests, then the study**

Run: `pytest tests/test_depth_lead_study.py -v`
Expected: PASS.
Run: `python scripts/depth_lead_study.py` against a local DB copy with Task 7 labels.
Expected: report written; median lead quoted verbatim in `STATE.md` and applied to Task 9's `exit_pacing_enabled` default before Task 9 commits (if Task 9 already landed, this study's result lands as a follow-up commit flipping the default, and says so).

- [ ] **Step 5: Commit**

```bash
git add scripts/depth_lead_study.py tests/test_depth_lead_study.py reports/depth-lead-study-*.json reports/depth-lead-study-*.md STATE.md
git commit -m "research(exit): depth-lead study — median lead of quote depth vs EV peak (Task 15)"
```

---

### Task 16: Distribution ensemble + holder-delta verification (spec §4, layers 3 + 5; §9 step 16)

Spec §4. The distribution layer needs holder **balance deltas** — top-holder net flow is a flow, and counts cannot show it. Step 1 of this task is therefore a verification, not a build, and the task branches on its finding.

**Files:**
- Modify: `features/definitions.py`, `features/factory.py` (distribution features)
- Modify: `trading/exit_policy.py` (accelerate votes)
- Modify (branch B only): `ingestion/holder_tracker.py`, `ingestion/service.py` (delta capture at ingest)
- Test: `tests/test_distribution_features.py` (create)

**Interfaces:**
- Consumes: `storage.models.Holder` (`asset_id`, `wallet_address`, `ts`, `source_id`, `balance`, `pct_supply` — unique per asset/wallet/ts/source); `storage.models.HolderMetric` (`top10_pct`, `holder_count`, `holder_growth_per_hour`); `ingestion/holder_tracker.py::get_solana_holders` / `get_evm_holders` / `record_holder_metrics`; `ingestion/service.py::_ingest_solana_holder_snapshots` / `_ingest_evm_holder_snapshots`; Task 9's `ExitPolicy` / `ExitSnapshot`.
- Produces:
  - The Step 1 verification finding, written into `STATE.md` and the study note in `reports/`: whether per-wallet balance deltas are (a) stored, (b) reconstructible, or (c) absent. **Pre-reading (plan-time, to be confirmed in Step 1):** `holders` stores a `balance` per (asset, wallet, ts) snapshot — deltas are *not* a stored column, but are the difference of consecutive per-wallet balances, so they are reconstructible point-in-time **where holder-snapshot density suffices**. Step 1 measures that density over the episode population and branches:
    - **Branch A (deltas reconstructible):** first deliverable is a delta derivation — `features/factory.py` helper computing per-wallet Δbalance between consecutive snapshots ≤ decision_ts — and the features below read it.
    - **Branch B (deltas absent / density insufficient):** the task's first deliverable becomes **delta capture**: persist per-wallet balance deltas at ingest time in the `holder_tracker` / service holder-snapshot path (new rows in `holders`' companion shape or a derived table via the next migration number), and the features below are built against the capture with their live evaluation deferred until deltas accrue — the task says so in `STATE.md` rather than grading the ensemble on reconstruction gaps.
  - Features (all point-in-time, factory idiom): `effort_vs_result_depth_normalized` (volume against shrinking price response, normalized by current quote depth), `top_holder_net_flow` (sum of Δbalance over the top-10 holders by balance at decision time; negative = distribution), `holder_stall` (holder-count growth ≈ 0 while `top10_pct` concentration rises, from `holder_metrics`).
  - `ExitPolicy` gains the three features as **accelerate votes** on `ExitSnapshot` (new optional fields, defaulting to neutral): ≥ 2 of 3 votes active on an armed position pulls the next ladder tranche forward (pace multiplier on tranche size, capped by the existing depth cap); votes never arm the ladder by themselves and never fire an exit alone (spec §4 layer discipline).

- [ ] **Step 1: Verify holder-delta availability (the branch point)**

Read `storage/models.py` (`Holder`, `HolderMetric`), `ingestion/holder_tracker.py`, and the two holder-snapshot ingest methods in `ingestion/service.py`. Then measure on a local DB copy: per-asset holder-snapshot counts and distinct-ts density for assets with Task 7 episodes. Write the finding (branch A or B, with the density numbers) into `STATE.md` and `reports/holder-delta-verification-<YYYYMMDD>.md`.

Run: the density queries from the study note.
Expected: a named branch with numbers — not an impression.

- [ ] **Step 2: Write the failing tests**

```python
def test_top_holder_net_flow_negative_when_top10_sell(session):
    # Top-10 wallets' balances fall between the two snapshots before
    # decision while total holder count rises: net flow < 0.
    ...
    assert feats["top_holder_net_flow"].value < 0.0


def test_effort_vs_result_rises_on_distribution_pattern(session):
    # Volume up 3x over the window, price response per unit volume
    # down, depth flat: effort-vs-result (depth-normalized) rises.
    ...


def test_accelerate_votes_pull_tranche_forward_only_when_armed(policy, state):
    # 2 of 3 votes active but position NOT armed: no tranche.
    # Same votes on an armed position: next tranche is pulled forward.
    ...
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_distribution_features.py -v`
Expected: FAIL — features and votes do not exist.

- [ ] **Step 4: Implement per the Step 1 branch**

Branch A: delta derivation in the factory, then the three features, then the policy votes. Branch B: delta capture first (its own tests: a second holder snapshot for a wallet persists a delta row; point-in-time reads never see deltas from snapshots observed after decision), then the same features/votes against the capture.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_distribution_features.py tests/test_exit_policy.py tests/test_feature_completeness.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add features/definitions.py features/factory.py trading/exit_policy.py tests/test_distribution_features.py STATE.md reports/holder-delta-verification-*.md
git commit -m "feat(exit): distribution features (effort-vs-result, top-holder net flow, holder stall) as accelerate votes (Task 16)"
```
(Branch B adds `ingestion/holder_tracker.py ingestion/service.py` and any migration to the add list.)

---

### Task 17: Exit-window liquidity label + cohort-conditional stage (spec §5/§8 label; §9 steps 9–10)

Spec §5, §8, §4. Two deliverables. **(a)** The harvest window's depth condition as a standalone, queryable label — "exit feasible for size X at t" — so policies can be screened on exitability without recomputing windows. **(b)** The cohort-conditional second-stage exit model: the existing phase-conditioned peak hazards promoted from a standalone score to the exit stack's second stage, trained only on the ignited cohort.

**Files:**
- Modify: `forecast/harvest.py` (exit-window label computation), `forecast/labels.py` (wiring)
- Modify: `ops/features_retention.py` (`MISSION_LABEL_TYPES` gains the exit-window types)
- Create: `forecast/cohort_exit.py`
- Modify: `trading/exit_policy.py` (completion layer consumes the cohort hazard)
- Modify: `common/config.py` (Settings: `exit_window_max_slippage_pct: float = 10.0`, `exit_window_volume_floor_share: float = 0.25`)
- Test: `tests/test_exit_window_labels.py` (create)

**Interfaces:**
- Consumes: Task 7's episode/depth machinery (`forecast.harvest`, `models.LiquiditySnapshot`); `trading/costs.py::simulate_sell(...)` and `slippage_pct(...)` for the cost leg; `forecast/engine.py`'s phase-conditioned peak hazards (`ForecastModel.peak_hazards_by_phase`, `_fit_phase_peak_hazards`) as the seed; the ignited cohort = decision points whose asset reached `IGNITION` or beyond (`pump_physics` phases / `ignition` label), never the full scored population.
- Produces:
  - Label constants in `forecast/labels.py`: `LABEL_EXIT_WINDOW_FEASIBLE_S02 = "exit_window_feasible_s02"`, `LABEL_EXIT_WINDOW_FEASIBLE_S10 = "exit_window_feasible_s10"`, `LABEL_EXIT_WINDOW_FEASIBLE_S25 = "exit_window_feasible_s25"` (binary `"1"`/`"0"`, written per decision point beside the Task 7 labels, and added to `MISSION_LABEL_TYPES` in this task). Pinned semantics: `exit_window_feasible_s{f}` at time `t` is `"1"` iff **all three** hold — (i) depth: `D(t) >= 0.50 * D_peak` (episode peak quote depth, Task 7 semantics); (ii) volume: trailing-window volume at `t` ≥ `exit_window_volume_floor_share` of the episode's peak trailing volume (the pool is still trading, not abandoned); (iii) cost: `simulate_sell` at notional `f * D_peak` against `D(t)` implies slippage ≤ `exit_window_max_slippage_pct`. Any leg failing → `"0"`. No depth/volume path → no label (absence, not zero), same rule as Task 7.
  - ```python
    # forecast/cohort_exit.py
    def fit_cohort_peak_hazards(session: Session, *, settings: Settings) -> dict[str, HazardFit]:
        """Phase-conditioned peak hazards refit on the IGNITED cohort
        only (decision points at/after the asset's IGNITION transition),
        reusing forecast.hazard.DiscreteHazardModel and the phase
        buckets of forecast.engine._fit_phase_peak_hazards. Fitting on
        the full population is the defect this stage exists to remove:
        pre-ignition rows teach the hazard what unignited coins do."""

    def harvest_window_closing_hazard(
        model: dict[str, HazardFit], *, phase: str, hours_in_position: float
    ) -> float: ...
    ```
    `ExitPolicy`'s completion layer (Task 9) gains this hazard as an input: hazard above a Settings threshold (`cohort_hazard_complete_threshold: float = 0.5`, added in this task) counts as the structural-confirmation vote alongside failed-retest structure; it does not fire an exit alone.

- [ ] **Step 1: Write the failing tests**

```python
def test_exit_window_label_requires_all_three_legs():
    # Depth at 60% of peak, volume at peak share, slippage inside the
    # cap -> "1". Same row with depth at 40% of peak -> "0". Same row
    # with volume decayed below the floor -> "0". Same row with
    # slippage over the cap at the s25 notional -> s25 "0", s02 "1".
    ...


def test_cohort_hazard_fit_excludes_pre_ignition_rows(session):
    # Samples from assets that never ignited must not enter the fit:
    # fitted cohort n equals the ignited-cohort n exactly.
    ...


def test_cohort_hazard_does_not_fire_exit_alone(policy, state):
    # Hazard above threshold on an unarmed, unconfirmed position:
    # action stays "hold" (it is a completion vote, not a trigger).
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_exit_window_labels.py -v`
Expected: FAIL — label constants, `forecast/cohort_exit.py` do not exist.

- [ ] **Step 3: Implement**

Label computation inside `forecast/harvest.py` beside the window logic (same aligned depth/volume rows; volume from the market-snapshot path Task 7 already aligns), wiring in both label engines, retention keep-set extension. Cohort stage: refit wrapper over the existing hazard machinery with the ignited-cohort filter at the sample loader; policy consumption as a completion vote.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exit_window_labels.py tests/test_harvest_labels.py tests/test_exit_policy.py tests/test_features_retention.py tests/test_forecast.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forecast/harvest.py forecast/labels.py forecast/cohort_exit.py ops/features_retention.py trading/exit_policy.py common/config.py tests/test_exit_window_labels.py tests/test_features_retention.py
git commit -m "feat(exit): exit-window liquidity labels + cohort-conditional peak-hazard stage (Task 17)"
```

---

## Sequencing notes (spec §9)

Dependency order is the task order, with the addendum tasks placed as follows: **0 (snapshot coverage) before everything** — Tasks 2/4/5/7 carry its Definition-of-Done constraint until the live coverage gate passes; 1 (retention) before 2 (labels) before 4 (replay) before 5 (entry rank); 2 before 6 (entry gate) and 7 (harvest); 7 before 8 (exit gate) before 9 (policy); 3 (hazard fix) is independent and may land any time before 6; 10 (priority reframe) after 5; 11 (surface) after 6 + 8; 12 is the long pole and never blocks 1–11; 13 (social prior) after 2 + 5 (it multiplies the composite and is falsified against the labels); 14 (buyer acceleration) after 5, alongside the entry programme; 15 (depth-lead study) after 7 and **before 9's pacing default is settled** (its result gates `exit_pacing_enabled`); 16 (distribution ensemble) after 9 (its votes plug into the policy), branching on its own Step 1 verification; 17 (exit-window label + cohort stage) after 7 + 9 (the label extends the harvest machinery; the cohort hazard feeds the policy's completion layer). Full-suite + `ruff` + `mypy` green at every commit is part of every task's Step 4, whether or not the step lists the whole suite.
