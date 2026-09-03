"""End-to-end: two ``python -m engine`` subprocesses against one SQLite file.

The single-writer guard in ``storage.database.acquire_sqlite_writer_lock`` (and
the engine's port-ownership check in ``engine.run.main``) must stop a second
engine from booting against the same ``serpent.db``. SQLite allows only one
writer; two writers contend until one wedges the loop on ``database is locked``
— the retention/phase wedge. This test drives the REAL ``python -m engine``
entrypoint in two subprocesses bound to a throwaway SQLite DB via
``SERPENT_DB_PATH`` (the same seam the rescore CLI e2e tests use) and asserts
the second exits 1 with a clear writer-conflict (or port-in-use) message while
the first keeps running.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Writer-conflict message from storage.database.acquire_sqlite_writer_lock.
LOCK_CONFLICT = "another process already holds the SQLite writer lock"
# Port-ownership message from engine.run.main.
PORT_IN_USE = "already in use"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Reserve a free TCP port, then release it for the subprocess to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env_for(db_path: Path, api_port: int, ui_port: int, archive_dir: Path) -> dict[str, str]:
    """Env that binds the engine to a throwaway DB + local, non-conflicting ports.

    Network work is trimmed (short request timeout, crawlers/LLM/probe off,
    long scan cadence) so the process stays light and terminates promptly; the
    test never depends on a scan completing. Unknown extra vars are harmless
    (``Settings`` uses ``extra="ignore"``).
    """
    return {
        **os.environ,
        "ENV": "local-single",
        "SERPENT_DB_PATH": str(db_path),
        "API_HOST": "127.0.0.1",
        "API_PORT": str(api_port),
        "UI_PORT": str(ui_port),
        "ARCHIVE_LOCAL_DIR": str(archive_dir),
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


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """Graceful SIGTERM first (the engine shuts its GUI/API down cleanly), then SIGKILL."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_second_engine_fails_fast_while_first_keeps_running(
    tmp_path: Path,
) -> None:
    """Two ``python -m engine`` subprocesses on one temp SQLite file.

    The first acquires the single-writer lock, bootstraps, and serves its API;
    the second, launched with identical env, must exit 1 immediately with the
    writer-conflict (or port-in-use) message instead of wedging the loop — and
    the first must still be alive.
    """
    db_path = tmp_path / "serpent.db"
    archive_dir = tmp_path / "archive"
    api_port = _free_port()
    ui_port = _free_port()
    env = _env_for(db_path, api_port, ui_port, archive_dir)

    # First engine: stdout/stderr go to a log file (never a PIPE, which could
    # deadlock on a chatty boot) so we can read it if boot fails.
    first_log = tmp_path / "engine1.log"
    with first_log.open("wb") as log_fh:
        first = subprocess.Popen(
            [sys.executable, "-m", "engine"],
            cwd=REPO_ROOT,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        try:
            ready = _wait_for_port("127.0.0.1", api_port)
            assert ready, (
                "first engine never brought up its API within 60s; poll()="
                f"{first.poll()}. Log:\n{first_log.read_text(errors='replace')}"
            )
            # Lock is acquired before the API binds, so once the port is up the
            # first engine provably holds the single-writer lock.
            assert first.poll() is None, (
                "first engine exited before the second even launched; "
                f"log:\n{first_log.read_text(errors='replace')}"
            )

            # Second engine: same env, must fail fast (writer conflict, or port
            # in use if it somehow got past the lock). Exits promptly.
            second = subprocess.run(
                [sys.executable, "-m", "engine"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

            combined = second.stdout + second.stderr
            assert second.returncode == 1, (
                f"second engine exited {second.returncode}, expected 1.\n"
                f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
            )
            assert LOCK_CONFLICT in combined or PORT_IN_USE in combined, (
                f"second engine did not fail with the single-writer message.\n"
                f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
            )

            # First engine untouched by the second's failed boot.
            assert first.poll() is None, (
                "first engine died when the second attempted boot; "
                f"log:\n{first_log.read_text(errors='replace')}"
            )
        finally:
            _terminate(first)
