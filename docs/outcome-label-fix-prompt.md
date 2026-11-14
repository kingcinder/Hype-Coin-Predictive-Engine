# Coder prompt: fix the unbounded outcome-label lookup in `risk_engine/outcomes.py`

## 0. What this is

A single, scoped fix. The red-team review of the out-of-sample validation proposal
found that the outcome labels the whole engine learns from can be contaminated by
events that happen AFTER the prediction window ends. Fix the label generation.
Do not redesign the validation protocol, do not touch band calibration or the
isotonic layer, do not change the window length. That work comes later, on top
of correct labels.

## 1. Repo and baseline

- Repo copy: `/media/cody/Data/DOCUMENTS_PROJECT_FILES/Hype-Coin-Predictive-Engine-main`
- Branch: `main`, HEAD at time of writing: `df938ab`
- Live DB in the same directory: `serpent.db` (alembic 0025). Do not delete it.
  Do not commit it. Do not commit the backups
  `serpent.db.pre-pitfix-2026-11-13` and `serpent.db.pre-r3-p12-2026-11-13`.
- A live engine process is running from this copy (PID 3825263, since 15:12).
  Do not kill, restart, or replace it. Do not start a second engine.
- Python: `.venv/bin/python`. Run the test suite with the venv on PATH.
- Pre-commit gitleaks hook binary is missing on this machine, so commits and
  pushes use `--no-verify` AFTER you run ruff check/format yourself and grep
  the diff for secrets. Never commit a secret, token, key, or password.

## 2. The bug (verified in the code, not assumed)

File: `risk_engine/outcomes.py` (392 lines).

`_latest_lifecycle_phase(session, asset_id)` (lines 59-68) returns the most
recent `LifecycleEvent` for an asset with NO timestamp bound:

```python
select(models.LifecycleEvent)
.where(models.LifecycleEvent.asset_id == asset_id)
.order_by(models.LifecycleEvent.ts.desc())
.limit(1)
```

`evaluate_outcomes` (lines 166+) gates eligibility on `scored_at + window`
(`window` = `settings.risk_outcome_window_hours`, default **48.0** hours,
`common/config.py:490` — use the configured window, do NOT hardcode 24h),
but then labels the prediction with that unbounded lookup:

```python
phase = _latest_lifecycle_phase(session, outcome.asset_id)
```

So: prediction recorded Day 0, window ends Day 2, token collapses Day 4,
evaluation runs Day 5 → the Day 0 prediction is labelled collapsed, even
though the token was alive for the whole window it was scored on. The label
claims knowledge from outside the observation window.

Same contamination class in `_price_change_since` (lines 70-114): the
"latest" snapshot after `since` has no upper bound, so a price days later can
stand in for the price at window end.

Downstream damage: these labels feed band calibration
(`scoring/band_calibration.py`), the validation harness metrics (RiskBand
survival, BLACK collapse precision, concordance), and the ensemble feedback
loop at the end of `evaluate_outcomes`, which trains scorer weights on them.
Bad labels poison all three.

Current outcome assignment (lines ~196-202) also has no "unresolved" state:
anything that is not collapse/dead/rugged/survivor/parabolic/saturation gets
`collapsed=False, survived=False` and `evaluated_at` set anyway, so it is
later counted as a non-survival by band calibration. An outcome that cannot
be established must remain unresolved, not silently become a failure.

## 3. The fix

1. Add a bounded lookup, e.g. `_lifecycle_phase_as_of(session, asset_id, as_of)`:
   latest `LifecycleEvent` with `ts <= as_of`. Use it everywhere a label is
   derived from lifecycle state.
2. In `evaluate_outcomes`, the observation interval for a prediction scored
   at `T0` is `[T0, T0 + window]`. The phase that labels it is the latest
   lifecycle event with `ts <= T0 + window`. Events after `T0 + window`
   must not determine the outcome.
3. Outcome states after the fix:
   - phase in (`collapse`, `dead`, `rugged`) within the window → collapsed
     (`rugged=True` only when phase is `rugged`).
   - phase in (`survivor`, `parabolic`, `saturation`) at window end → survived.
   - no lifecycle event at or before window end, or a phase outside the two
     sets above → **unresolved**: do NOT set `evaluated_at`; leave the row
     for a later evaluation pass. Record `lifecycle_phase_at_eval` as the
     bounded phase or `"unknown"`.
4. Bound `_price_change_since` the same way: the "latest" snapshot must be
   `<= window end` (add an `until` parameter). The entry snapshot stays the
   latest at or before `scored_at`.
5. In `record_risk_outcome`, bound the at-score phase lookup to
   `ts <= decision_ts` (defensive exactness; at insert time the latest event
   is normally already at or before now, but do not rely on that).
6. Do NOT rewrite historical `RiskOutcome` rows in place. This fix changes
   the evaluator going forward only. Re-deriving historical labels, if ever
   wanted, is a separate decision and must write to a new table or snapshot,
   never overwrite `RiskOutcome` in place.
7. Check the ensemble feedback block at the end of `evaluate_outcomes`:
   it already filters on `evaluated_at IS NOT NULL`, so unresolved rows are
   automatically excluded from training once step 3 is in place. Confirm
   that in your report; if any other consumer treats unevaluated rows as
   failures, say so explicitly instead of silently changing it.

## 4. Tests (new file `tests/test_outcomes.py` — no outcome tests exist yet)

Follow the repo's existing test conventions (in-memory SQLite, same fixture
style as the other storage/risk tests). Each test must set up its own
`LifecycleEvent` rows with explicit timestamps. Required cases:

1. Post-window collapse does NOT label the prediction: score Day 0, window
   ends Day 2, collapse event Day 4, evaluate Day 5 → `collapsed` is False
   and `evaluated_at` stays NULL (unresolved).
2. Collapse inside the window → `collapsed` True, `evaluated_at` set.
3. Rug inside the window → `rugged` True and `collapsed` True.
4. Survival phase present at window end → `survived` True.
5. No lifecycle events at all in the window → unresolved, `evaluated_at`
   stays NULL.
6. Price change bounded: a snapshot after window end is ignored; the
   `price_change_pct` uses the last snapshot at or before window end.
7. Boundary: an event exactly at `T0 + window` counts; an event 1 second
   after does not.

## 5. Verification and reporting

- `ruff check .` and `ruff format --check .` clean. Full pytest green.
  Known pre-existing exception: 2 launcher tests in
  `tests/test_start_engine_launcher.py` fail on this machine while the live
  engine runs (they trip `start-engine.sh`'s already-running check) and fail
  identically on the pristine baseline. Do not chase them; report them as
  pre-existing.
- Run the validation harness before and after the fix
  (`.venv/bin/python -m validation --db serpent.db --out reports/<name>.json`)
  and record both numbers in `docs/validation-field-report.md` (new section)
  and a short note in `STATE.md`. The numbers WILL change — that is the
  point. Report them honestly; do not tune anything to make them look better.
- Commit in small steps, one concern per commit, push to `origin main` only
  when pytest is green. Use `--no-verify` (see §1) after your own ruff check
  and a secret grep of each diff.
- Final report to the owner: files changed, commit hashes, test results,
  validation before/after numbers, and whether the ensemble feedback block
  needed any change beyond step 3.

## 6. Out of scope — do not do these

- Do not redesign P1/P2 or the validation proposal. P1 is already known to
  be infeasible as a confirmatory holdout; that is a separate document.
- Do not touch `scoring/band_calibration.py`, `risk_engine/confidence_calibration.py`,
  `risk_engine/calibrator.py`, or the fallback confidence table.
- Do not change `risk_outcome_window_hours` or its default.
- Do not add new dependencies, do not touch the live engine, do not touch
  the production DB file directly (all DB access through the app code and
  tests).
