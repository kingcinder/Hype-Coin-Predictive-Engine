"""End-to-end: writes must still land after boot, and Ctrl+C must stop the engine.

Regression for the boot-time write wedge. ``IngestionService.run_once`` used to
run its entire multi-minute scan inside one transaction, opened at the scan's
first write, and the post-scan phases (nightcrawler crawl, data-lake scoring and
label batches) each kept one transaction open for their whole run. SQLite allows
exactly one writer, so every one of those holds blocked the engine's write queue:
``system_health`` inserts waited out ``sqlite_busy_timeout_ms`` and were dropped
with ``database is locked`` — one warning every 30s — and if a source hung, the
transaction was never committed at all.

The tests drive the REAL ``python -m engine`` entrypoint against a throwaway
SQLite file (``SERPENT_DB_PATH``) and assert:

1. the engine persists new ``system_health`` rows after startup,
2. an independent connection can acquire the write lock,
3. one full post-scan phase cycle completes (forecast/retention/parity, crawl,
   then the data-lake pass) without the engine holding that lock for more than a
   few seconds at a time,
4. the engine log contains no ``write_queue_task_failed`` /
   ``write_queue_heartbeat_failed`` events across that cycle, and
5. a SIGINT mid-scan stops the process on its own, within a bounded time and
   with exit status 0, instead of needing SIGKILL.
"""

from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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

# One scan stage can sit inside a network call for a while on a slow runner, so
# the stop budget covers a worst-case stage plus the shutdown handshake (UI
# child, API thread, write-queue drain). The old behaviour never returned at all.
STOP_TIMEOUT_SECONDS = 60.0

STARTUP_TIMEOUT_SECONDS = 60.0


@dataclass
class _Engine:
    """A booted engine subprocess plus the paths its assertions look at."""

    proc: subprocess.Popen[bytes]
    db_path: Path
    log_path: Path

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")

    def alive(self) -> bool:
        return self.proc.poll() is None


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
    phases whose transaction hold the phase-cycle test covers (the other, the
    data-lake pass, is on by default). The token is supplied so
    ``ensure_api_token`` never has to write the repo .env.
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


@contextmanager
def _boot_engine(tmp_path: Path) -> Iterator[_Engine]:
    """Start ``python -m engine`` on a throwaway DB; always stop its group after."""
    api_port, ui_port = _free_port(), _free_port()
    db_path = tmp_path / "serpent.db"
    log_path = tmp_path / "engine.log"
    with log_path.open("wb") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "engine"],
            cwd=REPO_ROOT,
            env=_env_for(db_path, api_port, ui_port, tmp_path / "archive"),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        engine = _Engine(proc=proc, db_path=db_path, log_path=log_path)
        try:
            if not _wait_until(
                lambda: _port_accepts("127.0.0.1", api_port), STARTUP_TIMEOUT_SECONDS
            ):
                _fail(f"the API never came up in {STARTUP_TIMEOUT_SECONDS:.0f}s", engine)
            yield engine
        finally:
            _stop_group(proc)


def _port_accepts(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _wait_until(
    predicate: Callable[[], bool], timeout: float, engine: _Engine | None = None
) -> bool:
    """Poll ``predicate`` until it holds or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if engine is not None and not engine.alive():
            return False
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


def _health_rows(db_path: Path) -> int:
    """Count persisted ``system_health`` rows (0 before the table exists)."""
    if not db_path.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            return int(con.execute("select count(*) from system_health").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return 0


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
        now = time.monotonic()
        if _can_take_write_lock(db_path):
            if blocked_since is not None:
                longest = max(longest, now - blocked_since)
                blocked_since = None
        elif blocked_since is None:
            blocked_since = now
        time.sleep(0.25)
    if blocked_since is not None:
        longest = max(longest, time.monotonic() - blocked_since)
    return longest


def _markers(log_text: str) -> str:
    """Compact list of the engine lifecycle events seen, for failure reports."""
    interesting = (
        "engine_bootstrapped",
        "engine_scan_complete",
        "engine_scan_failed",
        "engine_forecast_training_complete",
        "engine_nightcrawler_complete",
        "engine_data_lake_complete",
        "engine_phase_skipped_still_wedged",
        "engine_nightcrawler_failed",
        "engine_data_lake_failed",
        "write_queue_task_failed",
        "write_queue_heartbeat_failed",
        "ingestion_scan_interrupted",
        "engine_stopped",
    )
    seen = [name for name in interesting if name in log_text]
    return f"events={seen}; nightcrawler_starts={log_text.count('nightcrawler_start')}"


def _fail(message: str, engine: _Engine) -> None:
    """Fail with a GitHub annotation: the job log is unreadable from the API.

    The annotation carries a compact reason (lifecycle events and the engine's
    exit state) instead of a multi-megabyte log tail, so a CI-only failure is
    diagnosable from the check-run without repository admin rights.
    """
    detail = f"{_markers(engine.log())}; engine_poll={engine.proc.poll()}"
    print(f"::error title=boot-writes-regression::{message} | {detail}", flush=True)
    raise AssertionError(f"{message}\n{detail}")


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    """Stop the engine and its Streamlit child (its own session/process group)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=30 if sig == signal.SIGTERM else 10)
        except subprocess.TimeoutExpired:
            continue


def test_engine_keeps_writing_after_boot(tmp_path: Path) -> None:
    """The booted engine must persist rows and never fence off the write lock."""
    with _boot_engine(tmp_path) as engine:
        # The engine is bootstrapped and its first scan is running. From here,
        # writes must keep landing and the lock must stay acquirable between
        # stage commits.
        if not _wait_until(
            lambda: _health_rows(engine.db_path) > 0 and _can_take_write_lock(engine.db_path),
            timeout=45.0,
            engine=engine,
        ):
            _fail(
                "no writes landed after boot, or the write lock stayed held "
                f"(rows={_health_rows(engine.db_path)}, "
                f"lock_free={_can_take_write_lock(engine.db_path)})",
                engine,
            )

        # ── One full post-scan phase cycle ────────────────────────────────────
        # Phase sessions are handed to the pipeline/pass, so a transaction opened
        # at the first crawl write or the first labelled batch used to stay open
        # until the phase returned.
        longest_hold = _wait_for_phase_cycle(engine.db_path, engine.log_path)
        log = engine.log()

        if "engine_data_lake_complete" not in log:
            _fail(
                "the post-scan phase cycle never reached the data-lake pass "
                f"within {PHASE_CYCLE_TIMEOUT_SECONDS:.0f}s",
                engine,
            )
        if "engine_nightcrawler_complete" not in log:
            _fail(
                "the nightcrawler phase did not complete, so the per-source "
                "commit path was not exercised",
                engine,
            )
        for event, what in (
            ("write_queue_task_failed", "the write queue lost work"),
            ("write_queue_heartbeat_failed", "the write-queue heartbeat could not write"),
        ):
            if event in log:
                _fail(
                    f"{what} during the phase cycle; longest observed hold={longest_hold:.1f}s",
                    engine,
                )
        if longest_hold >= MAX_PHASE_LOCK_HOLD_SECONDS:
            _fail(
                f"a phase held the write lock for {longest_hold:.1f}s "
                f"(budget {MAX_PHASE_LOCK_HOLD_SECONDS:.0f}s)",
                engine,
            )


def test_engine_stops_cleanly_on_sigint_mid_scan(tmp_path: Path) -> None:
    """SIGINT while a scan is running must stop the engine — no SIGKILL needed.

    Every scan stage is a unit of work, and the scan honours the shutdown event
    at each boundary. Before that, ``run_once`` was uninterruptible: Ctrl+C only
    set the event, and the process sat inside the remaining stages until the
    whole multi-minute scan finished, which is why the runbooks escalated to
    SIGKILL.
    """
    with _boot_engine(tmp_path) as engine:
        # A scan is demonstrably in flight once it has written rows and has not
        # logged its completion. A real scan cannot finish in the second or two
        # this takes, so the stop below always lands mid-scan.
        if not _wait_until(
            lambda: _health_rows(engine.db_path) > 0 and "engine_scan_complete" not in engine.log(),
            timeout=STARTUP_TIMEOUT_SECONDS,
            engine=engine,
        ):
            _fail("the first scan never started writing, so no mid-scan stop", engine)

        engine.proc.send_signal(signal.SIGINT)
        try:
            returncode = engine.proc.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _fail(
                f"still running {STOP_TIMEOUT_SECONDS:.0f}s after SIGINT mid-scan "
                "(the old SIGKILL-only behaviour)",
                engine,
            )
            raise  # unreachable: _fail always raises

        log = engine.log()
        if returncode != 0:
            _fail(f"clean stop exited {returncode} instead of 0", engine)
        if "engine_stopped" not in log:
            _fail("the engine never reached its shutdown epilogue", engine)
        if "ingestion_scan_interrupted" not in log:
            _fail(
                "the scan was not reported as interrupted: SIGINT did not reach a "
                "stage boundary, so shutdown still waited out the scan",
                engine,
            )
        if "ingestion_scan_failed" in log:
            _fail("the shutdown was recorded as a scan failure instead of an interruption", engine)
        if not _can_take_write_lock(engine.db_path, timeout=5.0):
            _fail("the write lock was still held after shutdown", engine)
