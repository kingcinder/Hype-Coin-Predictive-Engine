"""Retention-phase wedge diagnostic (ready-to-run ops script).

Drives the diagnosis in ``docs/runbook.md`` § "Retention-phase wedge" so an
operator can, in one command, see *why* the engine may be wedged:

- the recent ``retention_runs`` history and the **gap** vs the configured
  cadence (`RETENTION_CADENCE_HOURS`);
- the recent lake/archive ``system_health`` rows (and whether they fell
  **silent** — the classic wedge smell);
- any **phase-watchdog timeouts** (``*watchdog timeout; pass abandoned*``);
- whether a **competing writer** appears to own the SQLite DB (a second engine,
  or several processes holding the DB file open) or the **API port**.

Prints a human verdict and exits ``0`` (healthy), ``1`` (degraded / likely
wedged), or ``2`` (fatal: DB unreachable).

Usage::

    python -m scripts.diagnose_retention          # defaults from .env
    python -m scripts.diagnose_retention --db  /path/serpent.db
    python -m scripts.diagnose_retention --port 8000 --runs 5 --health 10
"""

from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

_SQLITE_MARKERS = ("sqlite+pysqlite:///", "sqlite:///")
# Health components that surface the lake's liveness/backlog/staleness.
_HEALTH_COMPONENTS = ("lake", "lake_backlog", "lake_stale_warning")
# Messages the phase watchdog writes when a stage is abandoned.
_WATCHDOG_LIKE = "%watchdog timeout; pass abandoned%"


def _sqlite_path(url: str) -> str | None:
    for marker in _SQLITE_MARKERS:
        if marker in url:
            return url.split(marker, 1)[1]
    return None


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _run(cmd: list[str]) -> list[str]:
    """Run a quick system utility; return its output lines or ``[]`` on any error."""
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=6, check=False)
        return (completed.stdout or "").splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return []


def _db_lock_held(path: str) -> bool:
    """True when another process holds the single-writer ``<db>.lock``.

    Non-destructive: opens + tries a non-blocking exclusive lock, then closes
    (readers like this script never keep a lock). Returns False when the lock
    is free *or* the backend isn't an on-disk SQLite engine we can lock.
    """
    if not path:
        return False
    import os

    lock_path = f"{path}.lock"
    if not os.path.exists(lock_path):
        return False
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows hosts.
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True  # another process holds it
    finally:
        os.close(fd)
    return False


def _processes_open(path: str) -> dict[str, str]:
    """PIDs + commands that currently have ``path`` open (``lsof``/``fuser``)."""
    pids: dict[str, str] = {}
    for line in _run(["lsof", str(path)]):
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            pids[parts[1]] = parts[0] if parts else "lsof"
    if pids:
        return pids
    for line in _run(["fuser", str(path)]):
        for tok in line.split():
            if tok.lstrip("-").isdigit():
                pids[tok.lstrip("-")] = "fuser"
    return pids


def _port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                return True
            return False
    except OSError:
        return True


def _port_owner(port: int) -> dict[str, str]:
    """PIDs + command names listening on ``port`` (``ss``/``lsof``/``fuser``)."""
    pids: dict[str, str] = {}
    for line in _run(["ss", "-ltnp"]):
        if f":{port}" not in line:
            continue
        pid_match = re.search(r"pid=(\d+)", line)
        name_match = re.search(r'users:\s*\(\s*"([^"]+)"', line)
        if pid_match:
            pids[pid_match.group(1)] = name_match.group(1) if name_match else "ss"
    if pids:
        return pids
    for line in _run(["lsof", f"-iTCP:{port}", "-sTCP:LISTEN"]):
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            pids[parts[1]] = parts[0] if parts else "lsof"
    if pids:
        return pids
    for tok in " ".join(_run(["fuser", "-n", "tcp", str(port)])).split():
        if tok.lstrip("-").isdigit():
            pids[tok.lstrip("-")] = "fuser"
    return pids


def _db_rows(engine: Any, sql: str, binds: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        result = connection.execute(text(sql), binds or {})
        columns = list(result.keys())
        return [dict(zip(columns, row, strict=True)) for row in result]


def _assess(
    *,
    database_url: str,
    api_port: int,
    cadence_hours: float,
    runs_limit: int,
    health_limit: int,
) -> int:
    print("Serpent retention-phase diagnostic")
    print("=" * 46)
    print(f"Database         : {database_url}")
    print(f"Retention cadence: {cadence_hours:.1f}h")
    db_path = _sqlite_path(database_url)
    print(f"SQLite file      : {db_path or '(not an on-disk SQLite engine)'}")

    flags: list[str] = []
    latest_run: datetime | None = None
    run_gap_hours: float | None = None
    last_health: datetime | None = None

    # ── DB history & health gap ───────────────────────────────────────────────
    try:
        engine = create_engine(database_url, poolclass=NullPool)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[!] Could not build an engine for {database_url}: {exc}")
        flags.append("DB unreachable")
        engine = None

    if engine is not None:
        try:
            runs = _db_rows(
                engine,
                "SELECT ts, archived_rows, byte_size, duration_sec "
                "FROM retention_runs ORDER BY ts DESC LIMIT :n",
                {"n": runs_limit},
            )
            print("\nRecent retention runs (ts / rows / bytes / dur_sec):")
            for row in runs or []:
                print(
                    f"  {row.get('ts')}  {row.get('archived_rows')}  "
                    f"{row.get('byte_size')}  {row.get('duration_sec')}s"
                )
            if runs:
                parsed = [_parse_ts(r.get("ts")) for r in runs]
                latest_run = parsed[0]
                if len(parsed) >= 2 and parsed[0] and parsed[1]:
                    run_gap_hours = (parsed[0] - parsed[1]).total_seconds() / 3600.0
                    delta = timedelta(hours=run_gap_hours)
                    print(f"\nGap between last two runs: {run_gap_hours:.2f}h ({delta})")
                    if run_gap_hours > cadence_hours * 1.5:
                        flags.append(
                            f"retention gap {run_gap_hours:.1f}h exceeds cadence "
                            f"{cadence_hours:.1f}h"
                        )
                elif latest_run:
                    age = (datetime.now(UTC) - latest_run).total_seconds() / 3600.0
                    print(f"\nOnly one run recorded; last pass {age:.2f}h ago.")
            else:
                print("\n[!] No retention_runs yet — the autopilot has not run.")
                flags.append("no retention passes recorded")

            health = _db_rows(
                engine,
                "SELECT ts, component, state, substr(message, 1, 70) AS message "
                "FROM system_health "
                "WHERE component IN (:c0, :c1, :c2) ORDER BY ts DESC LIMIT :n",
                {
                    "c0": _HEALTH_COMPONENTS[0],
                    "c1": _HEALTH_COMPONENTS[1],
                    "c2": _HEALTH_COMPONENTS[2],
                    "n": health_limit,
                },
            )
            print("\nRecent lake health (ts / component / state / message):")
            for row in (health or [])[:5]:
                print(
                    f"  {row.get('ts')}  {row.get('component'):<14} "
                    f"{row.get('state'):<6} {row.get('message')}"
                )
            if health:
                last_health = _parse_ts(health[0].get("ts"))
                if last_health is not None:
                    silent_hours = (datetime.now(UTC) - last_health).total_seconds() / 3600.0
                    if silent_hours > max(2.0, cadence_hours):
                        flags.append(
                            f"lake health silent for {silent_hours:.1f}h — "
                            "the loop likely wedged (no new health rows)"
                        )
                        print(
                            f"\n[!] Lake health has been silent for "
                            f"{silent_hours:.1f}h — classic wedge smell."
                        )

            alarms = _db_rows(
                engine,
                "SELECT ts, component, state, message FROM system_health "
                "WHERE message LIKE :pat ORDER BY ts DESC LIMIT 5",
                {"pat": _WATCHDOG_LIKE},
            )
            if alarms:
                print(
                    f"\n[!] {len(alarms)} phase-watchdog timeout(s) recorded "
                    f"(last at {alarms[0].get('ts')}, component "
                    f"{alarms[0].get('component')})."
                )
                flags.append("recent phase-watchdog timeout")
            else:
                print("\nNo recent phase-watchdog timeouts.")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[!] DB query failed: {exc}")
            flags.append("DB query failed")
        finally:
            engine.dispose()

    # ── Competing-writer / port ownership ─────────────────────────────────────
    print("\n--- Writer & port ownership ---")
    holder_pids: dict[str, str] = {}
    if db_path:
        holder_pids = _processes_open(db_path)
        lock_held = _db_lock_held(db_path)
        print(
            f"DB open by       : "
            f"{', '.join(f'{p}({c})' for p, c in holder_pids.items()) or 'none found'}"
        )
        print(f"Writer lock      : {'HELD (a writer is active)' if lock_held else 'free'}")
        if len(holder_pids) > 1:
            flags.append(
                f"{len(holder_pids)} processes have the DB open — possible competing writer"
            )

    owner = _port_owner(api_port)
    in_use = _port_in_use(api_port)
    print(
        f"API port {api_port}: "
        f"{'IN USE by ' + ', '.join(f'{p}({c})' for p, c in owner.items()) if owner else 'free'}"
    )
    if in_use:
        flags.append(f"port {api_port} already owned (likely a second engine / stale server)")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 46)
    if flags:
        print("VERDICT : DEGRADED / likely wedged")
        for flag in flags:
            print(f"  - {flag}")
        return 1
    print("VERDICT : OK — retention on cadence, no competing writer, no watchdog alarms.")
    if latest_run and cadence_hours:
        passed_ago = (datetime.now(UTC) - latest_run).total_seconds() / 3600.0
        print(f"Last retention pass {passed_ago:.2f}h ago (cadence {cadence_hours:.1f}h).")
    return 0


def main() -> int:
    from common.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Diagnose the retention-phase wedge (see docs/runbook.md)."
    )
    parser.add_argument("--db", default=settings.database_url, help="database URL or SQLite path")
    parser.add_argument("--port", type=int, default=settings.api_port, help="API port to check")
    parser.add_argument("--runs", type=int, default=5, help="recent retention runs to show")
    parser.add_argument("--health", type=int, default=10, help="recent lake health rows to query")
    args = parser.parse_args()

    db = args.db
    if isinstance(db, str) and not any(s in db for s in ("://", "sqlite")):
        db = f"sqlite:///{db}"

    return _assess(
        database_url=db,
        api_port=args.port,
        cadence_hours=settings.retention_cadence_hours,
        runs_limit=args.runs,
        health_limit=args.health,
    )


if __name__ == "__main__":
    sys.exit(main())
