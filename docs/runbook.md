# Operational Runbook

Field notes for diagnosing and recovering from incidents in the running engine.
Each section documents one real or anticipated failure mode: the symptom, the
root cause, and the recovery behavior built into the code (usually a watchdog
guard), plus the commands you run to confirm and recover.

---

## Retention-phase wedge (engine loop hangs in `retention`)

**Status:** recurring failure mode; mitigated by the phase watchdog
(`RETENTION_TIMEOUT_SECONDS` + `_run_watchdog_phase`) in `engine/run.py`.

### What the retention stage is

The retention autopilot compacts raw evidence older than
`ARCHIVE_COMPACT_AFTER_HOURS` (default `72`) into partitioned Parquet, prunes
expired rows, records a `RetentionRun` (lake totals + growth), and writes
`component:lake` health. It is driven three ways — pick one:

- **APScheduler** (docker profile): `ingestion/scheduler.py` registers a job on
  `RETENTION_CADENCE_HOURS` (default `24`).
- **Worker/engine loop** (zero-container profile): `ingestion/worker.py --loop`
  and `python -m engine` call `maybe_run_retention()` after each scan when
  `retention_due`.
- **OS scheduler**: `python -m ops.retention --once` from the systemd timer
  (`deploy/systemd/serpent-retention.timer`) or a Windows Task Scheduler entry.

It never raises: a failed pass records `component:lake` health as `red` and
returns `{"error": ...}`.

### Symptom

The engine stops making progress in the `retention` phase. In `engine.log` you
see a burst of `database is locked` errors, then **long silence**:

```
{"port": 8000, "event": "engine_api_not_started", ...}   # port already in use
{"event": "engine_scan_failed",   "error": "... database is locked ..."}
{"event": "engine_forecast_failed","error": "... database is locked ..."}
{"event": "engine_retention_failed","error": "... database is locked ..."}
{"event": "engine_parity_failed",  "error": "... database is locked ..."}
                                                          # ... then nothing
                                                          # for hours until SIGTERM
```

In the database, `retention_runs` shows a multi-hour gap that is *not* bounded
by the 24h cadence, and `system_health` for `component IN ('lake','lake_backlog')`
goes quiet:

```sql
-- expected: runs ~every 24h; a gap of many hours beyond that = wedge
SELECT ts, archived_rows, byte_size, duration_sec FROM retention_runs ORDER BY ts DESC LIMIT 5;
SELECT ts, component, state, substr(message, 1, 60) FROM system_health
  WHERE component IN ('lake','lake_backlog','lake_stale_warning')
  ORDER BY ts DESC LIMIT 10;
```

Before the watchdog fix, a wedged stage ran **synchronously in the engine's
main thread**, so the whole loop froze: no further scans, and the operational
watchdog itself (`run_watchdog` → WAL checkpoint / VACUUM / failure tracking)
never got to run.

### Root cause

Two writers against the same **SQLite** file (`sqlite:///serpent.db` in the
zero-container profile). SQLite allows only one writer at a time; with
`PRAGMA busy_timeout=5000`, each write contends for ~5s then raises
`database is locked`. The classic trigger is launching a second engine (or
leaving a stale server) that also binds port 8000 — visible as
`engine_api_not_started: address already in use` at boot — while the first is
already writing. Everything else (many reads + one writer) is fine in WAL mode;
the problem is two writers.

Less common but possible: one retention pass wedged for another reason (a hung
object-store/MinIO call or DuckDB read on a corrupt partition) holds the write
lock long enough to starve the loop.

### Recovery behavior (the watchdog)

Each blocking engine phase now runs inside a watchdog deadline instead of
blocking the loop forever:

- The stage is executed in a **daemon thread** (`run_stage_with_timeout` in
  `ops/watchdog.py`), and the loop waits at most `RETENTION_TIMEOUT_SECONDS`
  (default `600s`).
- On time-out the pass is **abandoned in the background** (a daemon thread, so
  it can never block shutdown), a red `component:lake` health row is recorded
  with message `retention stage exceeded Ns watchdog timeout; pass abandoned,
  engine loop continuing`, and the loop **continues** to the next iteration —
  scans and the operational watchdog keep running.
- The same guard covers forecast, parity, nightcrawler, and data-lake phases,
  each with its own timeout gated by `PHASE_TIMEOUT_SECONDS` (default `900`),
  `FORECAST_TIMEOUT_SECONDS`, `PARITY_TIMEOUT_SECONDS`,
  `DATA_LAKE_TIMEOUT_SECONDS`, and `NIGHTCRAWLER_TIMEOUT_SECONDS` (default
  `1800` for the crawl fleet). Set them in `.env`; a timed-out phase surfaces
  as a red health row and an `engine_stage_watchdog_timeout` log line.
- While a wedged run is still in flight, each subsequent iteration **skips**
  the phase instead of piling up daemon threads. To keep a long wedge from
  going silent after that first alarm, the phase **re-alerts** after
  `SKIP_ALERT_CYCLES` (default `20`) consecutive skips — another red
  `*watchdog timeout; pass abandoned*` row with message `… still wedged after
  N consecutive skipped iterations …`, plus an
  `engine_stage_watchdog_skip_realert` log line — then the counter resets and
  repeats every `SKIP_ALERT_CYCLES` until the wedge clears. Set `0` to disable
  re-alerting.

### Detection & recovery steps

1. **Confirm the wedge:** in `engine.log` look for the burst of `database is
   locked` across phases followed by silence; in the DB, confirm the
   `retention_runs` / `system_health` gap. Run the ready-made diagnostic
   (`python -m scripts.diagnose_retention`, or `make diagnose-retention`) — it
   prints the runs/health gap, any phase-watchdog timeouts, and flags whether a
   competing writer owns the DB or the API port, with a single VERDICT line.
2. **Identify the competing writer:** who owns the DB and port 8000.
   ```bash
   ss -lptn 'sport = :8000'          # or: lsof -iTCP:8000 -sTCP:LISTEN
   # find engine processes holding serpent.db open
   lsof serpent.db 2>/dev/null
   ```
   Two engines = the root cause. Keep exactly one `python -m engine` (or one
   `worker + api + ui`) process per `serpent.db`.
3. **Recover the loop:** with the watchdog in place the loop self-recovers (the
   wedged pass is abandoned); it does **not** need a restart. If you want a
   clean lake-growth record, once the competing writer is gone you can run one
   pass by hand: `make retention` or `python -m ops.retention --once`.
4. **Verify:** `component:lake` is `ok` again and `retention_runs` resumes on
   cadence. The red timeout row is the audit trail that a phase was abandoned;
   a recent alarm also flashes as a **live warning in the engine-status banner
   on every view**, plus prominent red panels in the **Feed Health** and
   **Archive & Retention** views (all via `GET /watchdog/alarms`), and the
   Feed Health component table colors red/yellow rows — so a wedged phase is
   visible without grepping logs.

   On top of those persisted rows, the **SSE feed** (`/engine/stream`) now
   carries **live per-phase watchdog state** (`watchdog.phases` in each
   snapshot): which phase is *currently* wedged (its abandoned run still in
   flight) and how many consecutive iterations have been skipped. The banner
   and the Feed Health / Archive & Retention panels render this live section, so
   a phase that is stuck *right now* — before the next persisted alarm — is
   visible immediately.

### Prevention

The code now **enforces** a single writer instead of relying on operator
discipline:

- **Single-writer lock guard:** on-boot, the engine and the worker loop call
  `acquire_sqlite_writer_lock()`, which takes an exclusive non-blocking `flock`
  on a sibling `serpent.db.lock` file and holds it for the process lifetime
  (released automatically by the OS on exit/crash — never stale). A second
  engine/worker against the same file fails fast with a clear
  `another process already holds the SQLite writer lock` message instead of
  starting up and wedging the loop. Non-SQLite backends (docker/Postgres) skip
  the guard entirely.
- **Port-ownership fail-fast:** `python -m engine` refuses to boot when
  `API_PORT` (default `8000`) is already bound `(address already in use)`, the
  exact symptom that used to let a second engine keep running.
- **Higher busy timeout:** inter-process contention is now prevented (above), so
  the remaining contention is intra-process (worker thread + API thread on one
  file). `SQLITE_BUSY_TIMEOUT_MS` (default `30000`) lets a genuine short
  collision wait ~30s instead of failing spuriously.

Seen together: `database is locked` loop wedges from a second engine should be
impossible — the second engine now refuses to start.

Remaining guidance:

- Tune `RETENTION_TIMEOUT_SECONDS` up only if a healthy pass legitimately takes
  long (e.g. a very large lake); prefer keeping the deadline tight so a wedge is
  abandoned early.
- Watch for corollary dryness: if `system_health` for `lake_backlog` and `lake`
  goes silent for hours while the process is up, re-check for contention.

---

## Score-distribution drift (GUI serves stale scores)

**Status:** guarded by the per-scan drift probe in `ops/score_drift.py`;
recovery is the existing rescore write pass, gated on an operator ack.

### What the drift alarm is

The GUI serves `scores.risk` rows straight from the database. When the
scoring formula changes (e.g. the proportional-risk rescore) the persisted
rows keep the OLD distribution until a write pass lands — historically the
served scores were quantized to a handful of bands while the live formula
produced hundreds of distinct values, and nothing flagged it. Each scan,
`maybe_run_score_drift()` samples the latest decision window (the exact rows
the GUI serves), re-runs the *current* `compute_scores` over the same feature
vectors, and grades the divergence:

- **KS D** — two-sample Kolmogorov–Smirnov statistic between the persisted
  and live risk distributions (pure numpy; scipy is not a dependency),
- **distinct-value ratio** — persisted distinct / live distinct: ≈1 equal
  richness, ≪1 means the served scores collapsed to bands,
- **mean |delta|** per token between persisted and live risk.

State machine: `ok` → no signal crossed warn; `yellow` → trending; `red` →
strong divergence, which records a `score_drift` SystemHealth row, opens or
re-arms a deduped `score_drift` Alert, and pages ntfy at most once per
`SCORE_DRIFT_ALERT_COOLDOWN_HOURS`. Every comparable probe also appends a
`score_drift_runs` row so `GET /score-drift/history` charts the divergence
growing *before* it crosses red.

### Alarm lifecycle (end-to-end)

One scan at a time, here is the whole arc from a formula change to a clean
bill of health:

1. **Formula changes, persisted scores lag.** The scoring formula is edited
   (or a rescore migration is skipped), but the `scores.risk` rows the GUI
   serves still hold the old distribution. Nothing alerts yet — the first
   probe has to *see* the gap.
2. **`yellow` — divergence trending (no page).** Once a probe has
   `SCORE_DRIFT_MIN_SAMPLES` scores to compare, any signal past its warn
   threshold (`score_drift_ks_d_warn`, `score_drift_distinct_ratio_warn`,
   `score_drift_mean_delta_warn`) classifies the probe `yellow`. Direction
   matters: KS D and mean |Δ| cross **up** as divergence grows, while the
   distinct ratio crosses **down** (≈1 healthy → ≪1 collapsed to bands). It appends a
   `score_drift_runs` row and records `yellow` health, but opens **no** alert
   and sends **no** ntfy push. The GUI card shows ⚠️ with a
   `make rescore-compare` hint; the trend sparkline starts to bend.
3. **`red` — alarm fires.** A strong signal (or combination) crosses the red
   thresholds (`score_drift_ks_d_red`, `score_drift_ks_p_red`,
   `score_drift_distinct_ratio_red`, `score_drift_mean_delta_red`) — same
   directions as above, so a *rising* ratio is not itself red, and the red
   field run's ratio stayed ≈1.03 because its KS D / mean |Δ| crossed. The probe:
   - records a **red** `score_drift` SystemHealth row,
   - opens (or re-arms) the **deduped** `score_drift` alert — one alert row,
     never one per scan,
   - pages ntfy **at most once per** `SCORE_DRIFT_ALERT_COOLDOWN_HOURS` (a
     second red probe in the window records the row + health but is
     `pushed: false`),
   - appends the `score_drift_runs` row that shows the red point on the trend.
   The alert starts in `state=open`, `acked_at=NULL` — that NULL is the
   `--auto-apply` gate.
4. **Operator ack — the sign-off.** `POST /alerts/{alert_id}/ack` with
   `{"quality": "useful"}` (or `noise`) flips the alert to `state=acked` and
   sets `acked_at`. This is the human sign-off that applying the *current*
   formula to the stored rows is wanted. Nothing is written yet.
5. **Rescue — `--auto-apply`.** `python -m ops.score_drift --once
   --auto-apply` runs a probe and, only when it is `red`, calls the rescore
   write pass — but only if the alert is acked. Unacked + red → **exit 2**
   with `reason: "no acked score_drift alert …"` and nothing written (the
   gate holds; see the recorded field proof below). On success it:
   - rewrites the persisted scores through the existing rescore write pass,
   - closes the alert (`state=closed`),
   - writes a **green** `score_drift` health row with an explicit `ts` so it
     never collides with the red probe row written the same second
     (`system_health` is unique on `(component, ts)`).
   **Partial failure:** if the write pass reports `errors > 0`, the fleet is
   only half-fixed and is **never** marked resolved — the CLI exits **3**
   (distinct from the exit-2 gate), the alert is re-opened (`state=open`,
   `acked_at` cleared) with a `PARTIAL auto-apply` annotation, a **red**
   health row records the failure count (not the green proof-of-rescue), and
   a follow-up ntfy pages. Fix the write failures, re-ack, and retry; the
   rescue only closes the alert once a pass lands with zero errors.
6. **Green confirmation.** The next probe (or `make score-drift`) compares the
   rewritten persisted scores against the same formula: KS D 0.000 (p≈1),
   distinct ratio 1.00, mean |delta| 0.0 → `ok`, and `--strict` exits 0.

**What the resolution health row means:** the newest green `score_drift`
SystemHealth row (message ends in `| no drift`) is the *proof of rescue* — it
is written by `rescue_drift` at the moment the write pass lands, not by the
probe that merely noticed the fix. Until that green row exists, treat red as
current even if the CLI probe happened to print a red result earlier in the
same run.

### Symptom

- **Red banner on the Score-Distribution Drift card** (Health & Diagnostics)
  and the compact badge on Feed Health / Live Ops Console:
  *STALE SCORES — won't match the live formula* with a `make rescore-compare`
  hint.
- System Health shows the `score_drift` component `red`; a `score_drift`
  alert is open and an ntfy push fired (once per cooldown).
- The trend sparkline bends toward red across recent probes before the alarm
  trips.

### Root cause

A formula change landed but the persisted scores were not rewritten. This is
**expected** between migrations — the alarm's job is to make the staleness
visible instead of silently serving band-quantized risks.

### Detection & recovery steps (the operator path)

1. **See the card.** Health & Diagnostics → Score-Distribution Drift (or the
   badge on Feed Health / Live Ops Console). Red = the GUI is serving stale
   scores. The alarm arms on the next scan once at least
   `SCORE_DRIFT_MIN_SAMPLES` scores exist.
2. **Size the gap.**
   ```bash
   make rescore-compare            # top movers old → new risk
   make rescore-compare MIN_CHANGE=10 SYMBOL_FILTER=UNKNOWN
   ```
   Review the movers — never trust served scores blindly while red.
3. **Ack the alert** (the sign-off that applying the live formula to the
   stored rows is wanted): open the **Alerts** view and rate the `score_drift`
   alert (✓ Useful). The API endpoint is `POST /alerts/{id}/ack`.
4. **Rescue the persisted scores.** The ack gates the write pass:
   ```bash
   make score-drift AUTO_APPLY=1      # = python -m ops.score_drift --once --auto-apply
   ```
   Exits 2 when still red but unacked (the gate refused) — ack and re-run.
5. **Watch it go green.** The next probe (or `make score-drift`) confirms the
   distributions match: KS D 0.000, distinct ratio 1.00.

### Diagnosis commands (stale-score incidents)

Everything an operator needs, in one place — each is read-only unless noted:

```bash
make score-drift                        # one probe; prints JSON status
make score-drift STRICT=1               # same, exit 1 on red/yellow (CI)
make score-drift-history                # trend series: run_ts, state, KS D/p,
                                        #   distinct ratio, mean |delta| (newest first)
make rescore-compare                    # top movers old → new risk (dry-run)
make rescore-compare MIN_CHANGE=10 SYMBOL_FILTER=UNKNOWN

# API state (no DB poking needed — the GUI reads these too):
curl -s localhost:8000/score-drift/latest   # newest probe {state, ks_d, distinct_ratio, ...}
curl -s localhost:8000/score-drift/history  # trend series, newest first
curl -s "localhost:8000/alerts?limit=50" | jq '.[] | select(.alert_type=="score_drift")'

# Is the served distribution actually collapsed? Compare persisted vs live
# distinct risk values (the 406-vs-bands gap signature):
#   persisted distinct ≈ handful of bands; live distinct ≈ hundreds
python - <<'PY'
import sqlite3
con = sqlite3.connect("serpent.db")
print("persisted distinct bands:",
      con.execute("SELECT COUNT(DISTINCT ROUND(risk, 0)) FROM scores").fetchone()[0])
PY

# Ack (the sign-off that gates --auto-apply):
curl -s -X POST localhost:8000/alerts/<id>/ack -H 'Content-Type: application/json' \
     -d '{"quality": "useful"}'

# Rescue the persisted scores (WRITES; gated on the ack):
make score-drift AUTO_APPLY=1           # exit 2 when red but unacked
```

Read them in order: `score-drift`/`score-drift-history` answer *is it stale and
growing?*, `rescore-compare` sizes *how far the movers go*, the SQL answers
*is it the band-collapse signature?*, and the ack + `AUTO_APPLY` pair is the
rescue itself.

### Field run (one-shot / CI)

```bash
python -m ops.score_drift --once              # one probe, prints JSON
python -m ops.score_drift --once --strict     # CI: exit 1 on red/yellow
make score-drift STRICT=1                     # same via make
```

A failed probe never kills the caller: it records `red` health and returns
`{"error": ...}` (parity contract). The GUI card, the Feed Health / Live Ops
Console badge, and the trend series all read from the API endpoints
`/score-drift/latest` and `/score-drift/history`, so no manual DB poking is
needed to check state.

### Field run (2026-09-01, pre-rescore backup — recorded proof)

Run against the REAL pre-rescore snapshot `serpent.db.pre-rescore-20260901-024646`
(4859 scores quantized to 77 distinct bands, 170K feature rows) — the same
migration + probe + payload flow an operator follows after any rescore:

```bash
# 1. Migrate a copy of the backup to the drift schema (0020 = score_drift_runs).
cp serpent.db.pre-rescore-20260901-024646 /tmp/serpent-pre-rescore-drift.db
SERPENT_DB_PATH=/tmp/serpent-pre-rescore-drift.db \
    alembic -c storage/alembic.ini upgrade head

# 2. Probe it. Exit 1 from --strict IS the expected red trip.
SERPENT_DB_PATH=/tmp/serpent-pre-rescore-drift.db \
    python -m ops.score_drift --once --strict
# -> {"status": "red", "compared": 300, "ks_d": 0.7167, "ks_p": 9.26e-69,
#     "distinct_ratio": 1.029, "mean_abs_delta": 20.11,
#     "reasons": ["KS D=0.717 (p=9.26e-69)", "mean |delta| 20.1"]}  exit=1
```

What ok vs red means **after a rescue**: the probe compares the *persisted*
risk distribution (whatever the last write pass stored) against the *current*
formula re-run over the same feature vectors. A freshly-rescored fleet reads
`ok` — KS D 0.000 (p≈1), distinct ratio 1.00 (equal richness), mean |delta| 0.0
— so `--strict` exits 0. A stale fleet reads `red` when **any** strong signal
trips — KS D past the red threshold, distinct ratio collapsing toward ≪1 (the
band-quantization signature), or mean |delta| large. Red is driven by
whichever signal(s) cross, not all of them: the field run tripped on KS D + mean
delta (`KS D=0.717 / ratio 1.03 / mean |delta| 20.1` — its distinct ratio stayed
≈1 because the pre-rescore persisted values were merely shifted, not collapsed)
and `--strict` exits 1. The trend series (`make score-drift-history`) shows the
divergence growing across probes before it ever crosses red.

How the ntfy payload reads (captured verbatim on the field run; ntfy is
disabled in local envs so nothing was sent):

```
Score-distribution drift: the persisted risk the GUI serves diverges
from the current formula over a 300-token sample.
KS D=0.717 (p=9.26e-69), distinct-ratio=1.03, mean |delta|=20.1.
Drivers: KS D=0.717 (p=9.26e-69); mean |delta| 20.1.
Run `python scripts/rescore.py` before trusting the served scores.
# headers: Title "Serpent Circle - Score Drift Alarm", Priority 4, Tags warning
```

Every probe that produced a comparable sample also appended a `score_drift_runs`
row, so the working copy now carries the red trend + health rows + an open
`score_drift` alert (`acked_at` NULL — the `--auto-apply` gate, awaiting ack).

### Prevention

- Run the probe in CI (`make score-drift STRICT=1`) so a formula change that
  never lands a write pass fails the pipeline instead of silently serving
  stale scores.
- Rescue soon after the formula change lands, so the served distribution and
  the live formula never drift far enough to mislead the risk console.