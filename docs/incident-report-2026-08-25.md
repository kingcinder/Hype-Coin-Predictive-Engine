# Incident Report: Retention Wedge - 2026-08-25

## Observed Symptoms

### Engine Log (`engine.log`)
- Engine loop froze during `retention` phase with `database is locked` errors
- Burst of errors followed by **long silence** (hours) until SIGTERM
- API not started: `engine_api_not_started: address already in use`
- Scan failures: `engine_scan_failed`, `engine_forecast_failed`, `engine_retention_failed`, `engine_parity_failed`
- In `serpent.db`, `retention_runs` showed multi-hour gaps beyond the 24h cadence
- `system_health` for `component IN ('lake','lake_backlog','lake_stale_warning')` went quiet

### Database (`serpent.db`)
```sql
-- Expected: runs ~every 24h; a gap of many hours beyond that = wedge
SELECT ts, archived_rows, byte_size, duration_sec FROM retention_runs ORDER BY ts DESC LIMIT 5;

SELECT ts, component, state, substr(message, 1, 60) FROM system_health
  WHERE component IN ('lake','lake_backlog','lake_stale_warning')
  ORDER BY ts DESC LIMIT 10;
```

## Root Cause

**Two SQLite writers against the same `sqlite:///serpent.db` file.**

- SQLite allows only one writer at a time with `PRAGMA busy_timeout=5000`
- Classic trigger: launching a second engine that also binds port 8000
- First engine already writing → second engine contends for write lock → `database is locked` after ~5s
- Everything else (many reads + one writer) works fine in WAL mode
- The problem is specifically **two writers**, not SQLite itself

Less common: one retention pass wedged for another reason (hung object-store/MinIO call, corrupt DuckDB partition) holding the write lock long enough to starve the loop.

## Watchdog Mitigations

The phase watchdog (`_run_watchdog_phase` in `engine/run.py`) now protects all blocking engine phases:

### Consecutive-skip re-alert (count-based)
- **`skip_alert_cycles`** (default 20): After this many consecutive skipped iterations the phase re-alerts (fresh red watchdog row) so a long-running wedge keeps surfacing instead of going dark
- **`skip_alert_max_alerts`** (default 5): Bounds how many re-alert rows one episode may record
- **`skip_alert_max_minutes`** (default 720 / 12 hours): Bounds how long re-alerting continues, measured from the episode first skip. Whichever cap is hit first mutes further re-alerts.
- Note: an earlier wall-clock variant of this re-alerting was reverted in R3 P0-2 - it reinterpreted the skip-count threshold as seconds, so skip_alert_cycles=20 meant 20 seconds. The count semantics above are the current behaviour.

### Per-phase timeouts (existing)
- `PHASE_TIMEOUT_SECONDS` (default 900s): Shared ceiling for forecast, parity, data_lake, nightcrawler
- `forecast_timeout_seconds` (default 900s)
- `parity_timeout_seconds` (default 900s)
- `data_lake_timeout_seconds` (default 900s)
- `nightcrawler_timeout_seconds` (default 1800s)

### Behavior on timeout
- Stage executed in a **daemon thread** (`run_stage_with_timeout` in `ops/watchdog.py`)
- Loop waits at most the phase's timeout setting
- On time-out: pass abandoned in background (daemon thread won't block shutdown)
- Red `component:health` row recorded with message e.g. `"retention stage exceeded Ns watchdog timeout; pass abandoned, engine loop continuing"`
- Loop **continues** to next iteration — scans and operational watchdog keep running
- While a wedged run is still in flight, each subsequent iteration **skips** the phase instead of piling up daemon threads

### Single-writer enforcement
- The watchdog ensures only one engine instance writes to `serpent.db` at a time
- Engine boot checks for existing port 8000 binding and refuses to start if already in use
- `ENGINE_API_TOKEN` and `ENGINE_API_AUTH_BYPASS` gate API access

## What to Verify After Deploying the Fix

### 1. Engine loop continues after phase timeout
- Start the engine with a temp SQLite DB
- Force a retention timeout (e.g., by holding a write lock)
- Verify the engine **does not freeze** — scans continue, next phases run
- Check `engine.log` for `engine_stage_watchdog_timeout` log line

### 2. Per-phase wedge state exposed on `/engine/status`
- Query `GET /engine/status`
- Verify `watchdog.phases` is present and shows live per-phase in-flight/skip state
- Each phase should show its current status (running, skipped, timed_out)

### 3. Global banner warning is time-bounded
- The global red banner should clear once no phase has timed out within the configurable recent window (e.g., last 15 minutes)
- Verify it does not persist until 20 historical alarms accumulate
- Check the banner clears after the retention timeout window passes

### 4. ntfy push for phase watchdog timeout
- Ensure ntfy is enabled (`ntfy_enabled=true`, `ntfy_topic` set)
- When a phase watchdog timeout occurs, verify ntfy push is sent
- Check the ntfy message has `phase_wedge_timeout` alert type, priority 4, tag "alert"
- Verify cooldown gating: only one push per cooldown period (handled by `notified_at` on the alert row)

### 5. Runbook links and unit-file comments survive linting
- Verify `docs/runbook.md` references survive Markdown linting
- Check systemd unit-file comments in `deploy/systemd/` reference existing docs
- Run CI check that anchors referenced in systemd/INSTALL comments exist in `docs/runbook.md`

### 6. `/watchdog/alarms` + UI panels render correctly
- Launch engine against temp SQLite DB
- Force a retention timeout
- Verify `/watchdog/alarms` endpoint returns alarm data
- UI Feed Health view should show the wedged phase and its skip/timeout state

### 7. End-to-end SSE health feed
- Connect to SSE health feed
- Verify live per-phase in-flight/skip state is surfaced
- Banner shows phase currently wedged and skipping right now, not just past timeout alarms
