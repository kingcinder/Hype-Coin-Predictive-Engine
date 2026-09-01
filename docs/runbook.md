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