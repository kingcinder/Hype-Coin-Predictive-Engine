"""End-to-end: writes must still land after the engine has booted.

Regression for the boot-time write wedge. ``IngestionService.run_once`` used to
run its entire multi-minute scan inside one transaction, opened at the scan's
first write. SQLite allows exactly one writer, so that open transaction held the
write lock for the whole scan: the engine's write queue waited out
``sqlite_busy_timeout_ms`` and dropped every ``system_health`` row with
``database is locked`` (one warning every 30s), and if a source hung the scan
never committed at all — ``system_health`` froze at the boot rows and an
independent writer could not take the lock either.

The test drives the REAL ``python -m engine`` entrypoint against a throwaway
SQLite file (``SERPENT_DB_PATH``) and asserts, while the engine keeps running,
that:

1. the engine persists new ``system_health`` rows after startup, and
2. an independent connection can acquire the write lock.

Both fail on the wedged boot and pass once each scan stage ends its own
transaction.
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


def _free_port() -> int:
    """Reserve a free TCP port, then release it for the subprocess to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env_for(db_path: Path, api_port: int, ui_port: int, archive_dir: Path) -> dict[str, str]:
    """Env that binds the engine to a throwaway DB, local ports, and light work.

    Network sources are left enabled so the scan performs its real stages; the
    short request timeout and single retry keep each source failure quick. The
    token is supplied so ``ensure_api_token`` never has to write the repo .env.
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
        "NIGHTCRAWLER_ENABLED": "false",
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


def _can_take_write_lock(db_path: Path) -> bool:
    """True when an independent connection can BEGIN IMMEDIATE (then roll back)."""
    try:
        con = sqlite3.connect(str(db_path), timeout=1.0, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("ROLLBACK")
            return True
        finally:
            con.close()
    except sqlite3.Error:
        return False


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
    """The booted engine must persist new rows and must not hold the write lock."""
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
        finally:
            _terminate_group(proc)
