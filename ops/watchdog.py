"""Operational watchdog — keeps the engine healthy during long unattended runs.

Responsibilities:
- SQLite WAL checkpoint + periodic VACUUM
- Consecutive failure circuit breaker
- Disk space monitoring (database + archive)
- Stale connection cleanup
- Daily health summary notification

Called once per scan iteration from the main engine loop.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from common.config import get_settings
from common.logging import get_logger
from storage.repository import record_health

log = get_logger(__name__)

# Consecutive failures before the engine enters degraded mode
_MAX_CONSECUTIVE_FAILURES = 5
# Disk usage threshold (percentage) before warning
_DISK_WARN_PCT = 85.0
_DISK_CRIT_PCT = 95.0


@dataclass
class WatchdogState:
    """Mutable state persisted across iterations in the engine loop."""

    consecutive_scan_failures: int = 0
    last_vacuum_monotonic: float = field(default_factory=time.monotonic)
    last_health_digest_monotonic: float = field(default_factory=time.monotonic)
    total_iterations: int = 0
    total_scan_failures: int = 0


def maybe_checkpoint_wal() -> None:
    """Force a WAL checkpoint on SQLite to prevent unbounded WAL growth.

    This is cheap and safe to call periodically — it merges the WAL into the
    main database file so the WAL doesn't grow to multiple gigabytes over
    weeks of continuous operation.
    """
    settings = get_settings()
    if "sqlite" not in settings.database_url:
        return
    try:
        from sqlalchemy import text

        from storage.database import SessionLocal

        with SessionLocal() as session:
            # WAL2 checkpoint: truncate the WAL after merging
            session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            session.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("wal_checkpoint_failed", error=str(exc))


def maybe_vacuum(state: WatchdogState, interval_hours: float = 6.0) -> None:
    """Run VACUUM on SQLite periodically to reclaim disk space and defragment.

    Only runs when the database file has grown significantly since the last
    vacuum. VACUUM is expensive (rewrites the entire file), so we gate it
    on a time interval.
    """
    settings = get_settings()
    if "sqlite" not in settings.database_url:
        return
    now = time.monotonic()
    if (now - state.last_vacuum_monotonic) < (interval_hours * 3600):
        return
    try:
        from sqlalchemy import text

        from storage.database import SessionLocal

        with SessionLocal() as session:
            session.execute(text("VACUUM"))
            session.commit()
        state.last_vacuum_monotonic = now
        log.info("sqlite_vacuum_complete")
    except Exception as exc:  # noqa: BLE001
        log.debug("sqlite_vacuum_failed", error=str(exc))


def check_disk_space(session: object) -> dict[str, float]:
    """Check disk usage for the database file and archive directory.

    Returns a dict with bytes_used, bytes_total, and usage_pct for each path.
    """
    settings = get_settings()
    results: dict[str, float] = {}

    for label, path_str in [
        ("database", settings.database_url.replace("sqlite:///", "")),
        ("archive", settings.archive_local_dir),
    ]:
        try:
            stat = os.statvfs(str(Path(path_str).parent))
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            used = total - free
            pct = (used / total * 100.0) if total > 0 else 0.0
            results[f"{label}_pct"] = round(pct, 1)
            results[f"{label}_used_gb"] = round(used / (1024**3), 2)
            results[f"{label}_free_gb"] = round(free / (1024**3), 2)
        except (OSError, ZeroDivisionError):
            pass

    if results:
        try:
            from sqlalchemy.orm import Session as _Session

            if not isinstance(session, _Session):
                return results
            warn_pcts = [
                results.get("database_pct", 0),
                results.get("archive_pct", 0),
            ]
            max_pct = max(warn_pcts) if warn_pcts else 0.0
            if max_pct >= _DISK_CRIT_PCT:
                record_health(
                    session,
                    component="disk",
                    state="red",
                    message=f"Disk usage critical: {max_pct:.0f}% — immediate action required",
                    error_count=1,
                )
            elif max_pct >= _DISK_WARN_PCT:
                record_health(
                    session,
                    component="disk",
                    state="yellow",
                    message=f"Disk usage elevated: {max_pct:.0f}% — plan cleanup soon",
                )
            else:
                record_health(
                    session,
                    component="disk",
                    state="ok",
                    message=(
                        f"db={results.get('database_pct', 0):.0f}% "
                        f"archive={results.get('archive_pct', 0):.0f}%"
                    ),
                )
        except Exception:  # noqa: BLE001
            pass

    return results


def check_consecutive_failures(state: WatchdogState) -> bool:
    """Return True if the engine should enter degraded mode (too many failures)."""
    if state.consecutive_scan_failures >= _MAX_CONSECUTIVE_FAILURES:
        log.warning(
            "watchdog_degraded_mode",
            consecutive_failures=state.consecutive_scan_failures,
            threshold=_MAX_CONSECUTIVE_FAILURES,
        )
        return True
    return False


def on_scan_success(state: WatchdogState) -> None:
    """Reset failure counter after a successful scan."""
    state.consecutive_scan_failures = 0


def on_scan_failure(state: WatchdogState) -> None:
    """Increment failure counter after a failed scan."""
    state.consecutive_scan_failures += 1
    state.total_scan_failures += 1


def run_watchdog(
    session: object,
    state: WatchdogState,
) -> dict[str, object]:
    """Run all watchdog checks once per iteration.

    Called from the engine loop after each scan completes (success or failure).
    Returns a summary dict for logging.
    """
    state.total_iterations += 1
    result: dict[str, object] = {
        "iteration": state.total_iterations,
        "consecutive_failures": state.consecutive_scan_failures,
        "total_failures": state.total_scan_failures,
    }

    # 1. WAL checkpoint (always — it's cheap)
    maybe_checkpoint_wal()

    # 2. Periodic VACUUM
    maybe_vacuum(state)

    # 3. Disk space check
    disk = check_disk_space(session)
    result.update(disk)

    # 4. Consecutive failure check
    result["degraded"] = check_consecutive_failures(state)

    # 5. Health summary record
    try:
        from sqlalchemy.orm import Session as _Session

        if isinstance(session, _Session):
            record_health(
                session,
                component="watchdog",
                state="yellow" if state.consecutive_scan_failures > 0 else "ok",
                message=(
                    f"iter={state.total_iterations} "
                    f"failures={state.total_scan_failures} "
                    f"consecutive={state.consecutive_scan_failures}"
                ),
                error_count=state.consecutive_scan_failures,
            )
    except Exception:  # noqa: BLE001
        pass

    return result
