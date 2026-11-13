"""End-to-end: writes must still land after boot, through the whole phase cycle.

Regression for the boot-time write wedge. ``IngestionService.run_once`` used to
run its entire multi-minute scan inside one transaction, opened at the scan's
first write, and the post-scan phases (nightcrawler crawl, data-lake scoring and
label batches) each kept one transaction open for their whole run. SQLite allows
exactly one writer, so every one of those holds blocked the engine's write queue:
``system_health`` inserts waited out ``sqlite_busy_timeout_ms`` and were dropped
with ``database is locked`` — one warning every 30s — and if a source hung, the
transaction was never committed at all.

The test drives the REAL ``python -m engine`` entrypoint against a throwaway
SQLite file (``SERPENT_DB_PATH``) and asserts, while the engine keeps running:

1. the engine persists new ``system_health`` rows after startup,
2. an independent connection can acquire the write lock,
3. one full post-scan phase cycle completes (forecast/retention/parity, crawl,
   then the data-lake pass) without the engine holding that lock for more than a
   few seconds at a time, and
4. the engine log contains no ``write_queue_task_failed`` /
   ``write_queue_heartbeat_failed`` events across that cycle.
"""

from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# A phase may legitimately hold the write lock while it persists one source's or
# one batch's rows; those writes are millisecond-scale, and a slow shared runner
# adds only seconds. The defect this test catches held the lock for 18-70s at a
# time, so the bar sits far above one batch and well below one crawl loop.
MAX_PHASE_LOCK_HOLD_SECONDS = 10.0

# The crawl and the data-lake pass follow the scan. Both are bounded generously:
# a shared runner fetches the same sources much slower than a workstation, and the
# point of the bound is to fail a wedged phase instead of hanging forever.
PHASE_CYCLE_TIMEOUT_SECONDS = 600.0


def _free_port() -> int:
    """Reserve a free TCP port, then release it for the subprocess to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env_for(db_path: Path, api_port: int, ui_port: int, archive_dir: Path) -> dict[str, str]:
    """Env that binds the engine to a throwaway DB, local ports, and light work.

    Network sources are left enabled so the scan and the crawl phase perform their
    real stages; the short request timeout and single retry keep each source
    failure quick. The nightcrawler phase is enabled because it is one of the two
    phases whose transaction hold this test covers (the other, the data-lake pass,
    is on by default). The token is supplied so ``ensure_api_token`` never has to
    write the repo .env.
    """
    return {
        **os.environ,
        "ENV": "local-single",
        "SERPENT_DB_PATH": str(db_path),
        "API_HOST": "127.0.0.1",
        "API_PORT": str(api_port),
        "UI_HOST": "127.0.0.1",
        "UI_PORT": str(ui_port),
        "ARCHIVE_LOCAL_DIR": str(archive_dir),
        "ENGINE_API_TOKEN": "boot-writes-regression-token-not-a-secret",
        "SCAN_INTERVAL_SECONDS": "3600",
        "REQUEST_TIMEOUT_SECONDS": "2",
        "MAX_REQUEST_RETRIES": "1",
        "RPC_POOL_BACKGROUND_PROBE_ENABLED": "false",
        "NIGHTCRAWLER_ENABLED": "true",
        "NIGHTCRAWLER_TIMEOUT_SECONDS": "600",
        "LLM_ENABLED": "false",
        "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }


def _wait_for_port(host: str, port: int, timeout: float = 60.0) -> bool:
    """True once ``(host, port)`` accepts a TCP connection (engine API up)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(0.2)
    return False


def _health_rows(db_path: Path) -> int | None:
    """Count persisted ``system_health`` rows, or None while the DB is absent."""
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            return int(
                con.execute(
                    "select count(*) from sqlite_master where type='table' and name='system_health'"
                ).fetchone()[0]
                and con.execute("select count(*) from system_health").fetchone()[0]
                or 0
            )
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _can_take_write_lock(db_path: Path, timeout: float = 1.0) -> bool:
    """True when an independent connection can BEGIN IMMEDIATE (then roll back)."""
    try:
        con = sqlite3.connect(str(db_path), timeout=timeout, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("ROLLBACK")
            return True
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _wait_for_phase_cycle(db_path: Path, log_path: Path) -> float:
    """Watch one phase cycle; return the longest continuous lock hold (seconds).

    Polls an independent writer while the engine runs its post-scan phases, which
    finishes when the data-lake pass logs ``engine_data_lake_complete``. Every
    failed attempt is a moment the engine (or its write queue) held the write lock,
    so consecutive failures measure how long one transaction stayed open.
    """
    deadline = time.monotonic() + PHASE_CYCLE_TIMEOUT_SECONDS
    longest = 0.0
    blocked_since: float | None = None
    while time.monotonic() < deadline:
        if "engine_data_lake_complete" in log_path.read_text(errors="replace"):
            break
        acquired = _can_take_write_lock(db_path)
        now = time.monotonic()
        if acquired:
            if blocked_since is not None:
                longest = max(longest, now - blocked_since)
                blocked_since = None
        elif blocked_since is None:
            blocked_since = now
        time.sleep(0.25)
    if blocked_since is not None:
        longest = max(longest, time.monotonic() - blocked_since)
    return longest


def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
    """Stop the engine and its Streamlit child (its own session/process group)."""
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def test_engine_keeps_writing_after_boot(tmp_path: Path) -> None:
    """The booted engine must persist rows and never fence off the write lock."""
    db_path = tmp_path / "serpent.db"
    archive_dir = tmp_path / "archive"
    api_port = _free_port()
    ui_port = _free_port()
    engine_log = tmp_path / "engine.log"
    started_at = time.time()

    with engine_log.open("wb") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "engine"],
            cwd=REPO_ROOT,
            env=_env_for(db_path, api_port, ui_port, archive_dir),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            assert _wait_for_port("127.0.0.1", api_port), (
                "engine never brought up its API within 60s; poll()="
                f"{proc.poll()}. Log:\n{engine_log.read_text(errors='replace')}"
            )

            # The engine is now bootstrapped and its first scan is running. From
            # here, writes must keep landing and the lock must stay acquirable
            # between stage commits.
            deadline = time.monotonic() + 45.0
            rows = 0
            lock_free = False
            while time.monotonic() < deadline:
                assert proc.poll() is None, (
                    "engine exited during the observation window; "
                    f"log:\n{engine_log.read_text(errors='replace')}"
                )
                rows = _health_rows(db_path) or 0
                lock_free = _can_take_write_lock(db_path)
                if rows > 0 and lock_free:
                    break
                time.sleep(1.0)

            log_tail = engine_log.read_text(errors="replace")[-4000:]
            assert rows > 0, (
                "the engine wrote no system_health rows after boot: writes are "
                "still wedged behind an open scan transaction.\n"
                f"rows={rows} lock_free={lock_free}\n"
                f"log tail:\n{log_tail}"
            )
            assert lock_free, (
                "an independent connection could not take the write lock while the "
                "engine ran: a scan transaction is still held open.\n"
                f"rows={rows}\nlog tail:\n{log_tail}"
            )
            assert db_path.stat().st_mtime >= started_at - 1, (
                "the database was never written during this run"
            )

            # ── One full post-scan phase cycle ────────────────────────────────
            # Phase sessions are handed to the pipeline/pass, so a transaction
            # opened at the first crawl write or the first labelled batch used to
            # stay open until the phase returned.
            longest_hold = _wait_for_phase_cycle(db_path, engine_log)
            phase_log = engine_log.read_text(errors="replace")
            tail = phase_log[-6000:]

            assert "engine_data_lake_complete" in phase_log, (
                "the post-scan phase cycle never reached the data-lake pass within "
                f"{PHASE_CYCLE_TIMEOUT_SECONDS:.0f}s, so no full cycle was observed.\n"
                f"log tail:\n{tail}"
            )
            assert "engine_nightcrawler_complete" in phase_log, (
                "the nightcrawler phase did not complete, so the per-source commit "
                f"path was not exercised.\nlog tail:\n{tail}"
            )
            assert "write_queue_task_failed" not in phase_log, (
                "the write queue lost work during the phase cycle: a phase is still "
                "holding SQLite's write lock long enough to time out an insert.\n"
                f"longest observed hold={longest_hold:.1f}s\nlog tail:\n{tail}"
            )
            assert "write_queue_heartbeat_failed" not in phase_log, (
                "the write-queue heartbeat could not write during the phase cycle, "
                "which means the lock was held across it.\n"
                f"longest observed hold={longest_hold:.1f}s\nlog tail:\n{tail}"
            )
            assert longest_hold < MAX_PHASE_LOCK_HOLD_SECONDS, (
                f"a phase held the write lock for {longest_hold:.1f}s "
                f"(budget {MAX_PHASE_LOCK_HOLD_SECONDS:.0f}s): commit per source in "
                "the crawl and per batch in the data-lake pass.\n"
                f"log tail:\n{tail}"
            )
        finally:
            _terminate_group(proc)
