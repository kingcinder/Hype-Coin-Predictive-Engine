"""Single-process engine supervisor.

``python -m engine`` bootstraps the zero-container database, then runs the whole
prediction engine from one process:

  - the ingestion worker loop (scan -> forecast training -> retention) in the
    main thread, on the same scan/backoff cadence as ``ingestion.worker --loop``;
  - the FastAPI REST layer as a uvicorn server in a daemon thread;
  - the Streamlit GUI as a supervised subprocess (Streamlit manages its own
    server and cannot run in-process).

Ctrl+C / SIGTERM stop the worker, the API, and the GUI cleanly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.config import Settings
    from ops.watchdog import StageOutcome

from common.logging import get_logger

log = get_logger(__name__)


def _run_migrations() -> None:
    """Apply the alembic migration chain to the database on every boot.

    ``Base.metadata.create_all`` only creates missing *tables* — it never alters
    existing tables when models gain columns.  The live drift (migration 0019's
    ML probability thresholds missing from ``risk_calibrations``/``risk_outcomes``)
    happened exactly because the engine booted with create_all only.  Every
    migration in the chain is existence-guarded, so ``upgrade head`` is
    idempotent: fresh DBs build the full schema, existing DBs get only the
    pending column/table additions.
    """
    try:
        from alembic import command
        from alembic.config import Config

        project_root = Path(__file__).resolve().parents[1]
        cfg = Config(str(project_root / "storage" / "alembic.ini"))
        cfg.set_main_option("script_location", str(project_root / "storage" / "migrations"))
        command.upgrade(cfg, "head")
        log.info("engine_migrations_applied")
    except Exception as exc:  # noqa: BLE001 - never block engine boot on migration issues.
        log.warning("engine_migrations_failed", error=str(exc))


def _bootstrap() -> None:
    """Idempotent zero-container bootstrap: migrations + schema + reference rows."""
    from common.config import get_settings
    from storage.database import Base, SessionLocal, engine
    from storage.seed import seed_reference_data

    settings = get_settings()
    Path(settings.archive_local_dir).mkdir(parents=True, exist_ok=True)
    # Migrations first — create_all is only a safety net for anything the models
    # define that the chain does not (it cannot add columns to existing tables).
    _run_migrations()
    Base.metadata.create_all(bind=engine)
    seed_reference_data()
    with SessionLocal() as session:
        session.commit()
    log.info(
        "engine_bootstrapped",
        database_url=settings.database_url,
        archive_dir=settings.archive_local_dir,
    )


def _wait_for_api(server, stop: threading.Event, timeout_seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while not server.started and time.monotonic() < deadline and not stop.is_set():
        time.sleep(0.1)
    return server.started


def _port_in_use(host: str, port: int) -> bool:
    """True when a socket on ``(host, port)`` is already bound by another process.

    Cheap pre-bind check so the engine fails fast at boot with a clear message
    instead of silently continuing past ``address already in use`` and wedging
    the loop against a second writer.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            sock.bind((host, port))
        except OSError:
            return True
        return False


def _run_watchdog_phase(
    *,
    stage: str,
    component: str,
    timeout_seconds: float,
    fn: Callable[[], dict[str, object]],
    session: Any = None,
) -> StageOutcome:
    """Run a blocking engine phase under the shared watchdog timeout.

    Mirrors the retention-stage guard: the phase runs synchronously in the
    engine's main thread, so if it stops making progress it would freeze the
    whole loop — no further scans and no operational watchdog.
    ``run_stage_with_timeout`` runs it in a daemon thread with a wall-clock
    deadline. Returns the ``StageOutcome``: on a fresh timeout a red health
    alarm for ``component`` is recorded and the loop continues; on a skip (a
    previous wedged run of the same phase still in flight) nothing is recorded
    because the original timeout already alarmed.

    ``session`` is injectable for tests: when provided the alarm row is added
    to it (and flushed) instead of opening a throwaway ``SessionLocal``, so an
    integration driver can assert the red row landed.
    """
    from ops.watchdog import run_stage_with_timeout
    from storage.repository import record_health

    outcome = run_stage_with_timeout(fn, timeout_seconds=timeout_seconds, stage=stage)
    if not outcome.timed_out:
        return outcome

    message = (
        f"{stage} stage exceeded {timeout_seconds:.0f}s watchdog timeout; "
        "pass abandoned, engine loop continuing"
    )
    log.error("engine_stage_watchdog_timeout", stage=stage, timeout_seconds=timeout_seconds)
    if session is not None:
        record_health(session, component=component, state="red", message=message)
        session.flush()
    else:
        from storage.database import SessionLocal

        try:
            with SessionLocal() as alarm_session:
                record_health(alarm_session, component=component, state="red", message=message)
                alarm_session.commit()
        except Exception as exc:  # noqa: BLE001 - an alarm must never kill the loop.
            log.debug("stage_watchdog_alarm_failed", stage=stage, error=str(exc))
    return outcome


def run_engine_phases(
    *,
    settings: Settings,
    iteration: int,
    stop: threading.Event,
    last_nc_run_monotonic: float,
    system_state: Any = None,
    alarm_session: Any = None,
    forecast_fn: Callable[[], dict[str, object]] | None = None,
    retention_fn: Callable[[], dict[str, object]] | None = None,
    parity_fn: Callable[[], dict[str, object]] | None = None,
    nightcrawler_fn: Callable[[], dict[str, object]] | None = None,
    data_lake_fn: Callable[[], dict[str, object]] | None = None,
) -> tuple[bool, float]:
    """Run the five post-scan engine phases for one loop iteration.

    This is the phase wiring that ``main()`` drives each iteration — forecast
    training, retention compaction, parity, night crawler, and the data-lake
    pass — each guarded by the watchdog timeout. Every phase callable is
    injectable (defaults to the real pipeline) and alarm rows can be written to
    an injected ``alarm_session`` instead of the live DB, so tests can drive the
    wiring end-to-end with blocking stubs. Returns ``(phase_error,
    last_nc_run_monotonic)``. ``phase_error`` is True when any phase (except
    parity) failed or was abandoned by the watchdog; ``last_nc_run_monotonic``
    advances only on a completed night-crawler pass.
    """
    from engine.state import engine_state
    from forecast.engine import maybe_run_forecast
    from ops.parity import maybe_run_parity
    from ops.retention import maybe_run_retention

    state = system_state or engine_state
    forecast_fn = forecast_fn or maybe_run_forecast
    retention_fn = retention_fn or maybe_run_retention
    parity_fn = parity_fn or maybe_run_parity

    if nightcrawler_fn is None:

        def _nightcrawl_default() -> dict[str, object]:
            from crawlers.pipeline import run_nightcrawler_pipeline
            from storage.database import SessionLocal

            with SessionLocal() as session:
                result = run_nightcrawler_pipeline(session)
                session.commit()
                return result

        nightcrawler_fn = _nightcrawl_default
    if data_lake_fn is None:

        def _data_lake_default() -> dict[str, object]:
            from data_lake.manager import run_data_lake_pass
            from storage.database import SessionLocal

            with SessionLocal() as session:
                result = run_data_lake_pass(session)
                session.commit()
                return result

        data_lake_fn = _data_lake_default

    phase_error = False

    # ── Forecast training ────────────────────────────────────────────────────
    try:
        state.mark_forecasting()
        forecast = _run_watchdog_phase(
            stage="forecast",
            component="forecast",
            timeout_seconds=settings.forecast_timeout_seconds,
            fn=forecast_fn,
            session=alarm_session,
        )
        if forecast.timed_out:
            state.mark_error("forecast: watchdog timeout")
            phase_error = True
        elif forecast.skipped:
            log.info("engine_phase_skipped_still_wedged", stage="forecast")
        elif forecast.result and forecast.result.get("status") != "skipped":
            log.info("engine_forecast_training_complete", result=forecast.result)
    except Exception as exc:  # noqa: BLE001 - forecast failure must not kill the engine
        log.exception("engine_forecast_failed", iteration=iteration, error=str(exc))
        state.mark_error(f"forecast: {exc}")
        phase_error = True

    # ── Retention compaction + pruning ───────────────────────────────────────
    try:
        state.mark_retention()
        retention = _run_watchdog_phase(
            stage="retention",
            component="lake",
            timeout_seconds=settings.retention_timeout_seconds,
            fn=retention_fn,
            session=alarm_session,
        )
        if retention.timed_out:
            state.mark_error("retention: watchdog timeout")
            phase_error = True
        elif retention.skipped:
            log.info("engine_phase_skipped_still_wedged", stage="retention")
        elif retention.result and not retention.result.get("skipped"):
            log.info("engine_retention_complete", result=retention.result)
    except Exception as exc:  # noqa: BLE001 - retention failure must not kill the engine
        log.exception("engine_retention_failed", iteration=iteration, error=str(exc))
        state.mark_error(f"retention: {exc}")
        phase_error = True

    # ── Lake-vs-SQL parity CI ────────────────────────────────────────────────
    try:
        parity = _run_watchdog_phase(
            stage="parity",
            component="parity",
            timeout_seconds=settings.parity_timeout_seconds,
            fn=parity_fn,
            session=alarm_session,
        )
        if parity.skipped:
            log.info("engine_phase_skipped_still_wedged", stage="parity")
        elif parity.result and not parity.result.get("skipped"):
            log.info("engine_parity_complete", result=parity.result)
    except Exception as exc:  # noqa: BLE001 - parity failure must not kill the engine
        log.exception("engine_parity_failed", iteration=iteration, error=str(exc))

    # ── Night Crawler pass (interval-gated) ─────────────────────────────────
    try:
        if settings.nightcrawler_enabled and not stop.is_set():
            nc_interval_sec = settings.nightcrawler_interval_minutes * 60
            now_mono = time.monotonic()
            if (now_mono - last_nc_run_monotonic) >= nc_interval_sec:
                nc_result = _run_watchdog_phase(
                    stage="nightcrawler",
                    component="nightcrawler",
                    timeout_seconds=settings.nightcrawler_timeout_seconds,
                    fn=nightcrawler_fn,
                    session=alarm_session,
                )
                if nc_result.timed_out:
                    state.mark_error("nightcrawler: watchdog timeout")
                    phase_error = True
                elif nc_result.skipped:
                    log.info("engine_phase_skipped_still_wedged", stage="nightcrawler")
                else:
                    last_nc_run_monotonic = now_mono
                    log.info(
                        "engine_nightcrawler_complete",
                        iteration=iteration,
                        result=nc_result.result,
                    )
    except Exception as exc:  # noqa: BLE001
        log.exception("engine_nightcrawler_failed", iteration=iteration, error=str(exc))
        state.mark_error(f"nightcrawler: {exc}")
        phase_error = True

    # ── Data lake pass ───────────────────────────────────────────────────────
    try:
        if settings.data_lake_enabled:
            dl_result = _run_watchdog_phase(
                stage="data_lake",
                component="data_lake",
                timeout_seconds=settings.data_lake_timeout_seconds,
                fn=data_lake_fn,
                session=alarm_session,
            )
            if dl_result.timed_out:
                state.mark_error("data_lake: watchdog timeout")
                phase_error = True
            elif dl_result.skipped:
                log.info("engine_phase_skipped_still_wedged", stage="data_lake")
            else:
                log.info(
                    "engine_data_lake_complete",
                    iteration=iteration,
                    result=dl_result.result,
                )
    except Exception as exc:  # noqa: BLE001 - data lake failure must not kill the engine
        log.exception("engine_data_lake_failed", iteration=iteration, error=str(exc))
        state.mark_error(f"data_lake: {exc}")
        phase_error = True

    return phase_error, last_nc_run_monotonic


def main() -> None:
    from common.config import get_settings
    from storage.database import acquire_sqlite_writer_lock

    # Single-writer guard: fail fast at boot if another engine process already
    # owns the SQLite file, rather than contending for SQLite's single write
    # lock and wedging the loop on "database is locked".
    _db_lock: int | None = None
    try:
        _db_lock = acquire_sqlite_writer_lock(get_settings())
    except RuntimeError as exc:  # noqa: BLE001
        log.critical("engine_sqlite_writer_conflict", error=str(exc))
        print(str(exc))
        raise SystemExit(1) from None

    _bootstrap()

    settings = get_settings()

    stop = threading.Event()

    def _handle_stop(signum: int, _frame: object) -> None:
        log.info("engine_shutdown_signal", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    # --- REST API in a daemon thread -----------------------------------------
    # Fail fast if the API port is already owned by another process: silently
    # continuing is what originally let a second engine keep running, contending
    # for the DB write lock and wedging the loop.
    if _port_in_use(settings.api_host, settings.api_port):
        log.critical(
            "engine_api_port_in_use",
            host=settings.api_host,
            port=settings.api_port,
        )
        print(
            f"\nERROR: port {settings.api_port} is already in use — another "
            "engine (or a stale server) is likely running against the same DB. "
            "Refusing to start to avoid wedging the loop with a second writer.\n"
        )
        raise SystemExit(1) from None

    import uvicorn

    api_server = uvicorn.Server(
        uvicorn.Config(
            "api.main:app",
            host=settings.api_host,
            port=settings.api_port,
            log_level="info",
        )
    )
    api_thread = threading.Thread(target=api_server.run, name="engine-api", daemon=True)
    api_thread.start()
    if not _wait_for_api(api_server, stop):
        log.warning("engine_api_not_started", port=settings.api_port)
        raise SystemExit(
            f"API server failed to bind port {settings.api_port} "
            "(another engine may already be running on it)."
        ) from None

    # --- Streamlit GUI as a supervised subprocess -----------------------------
    ui_path = Path(__file__).resolve().parents[1] / "ui" / "app.py"
    ui = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(ui_path),
            "--server.port",
            str(settings.ui_port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ],
        env={**os.environ, "API_BASE_URL": settings.api_base_url},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print(
        "\nSerpent Circle engine is up.\n"
        f"  API : http://localhost:{settings.api_port}/health\n"
        f"  GUI : http://localhost:{settings.ui_port}\n"
        "  Ctrl+C to stop.\n"
    )

    # --- Ingestion worker loop in the main thread -----------------------------
    from engine.state import engine_state
    from ingestion.service import backoff_sleep_seconds
    from ingestion.source_clients import ensure_background_probe
    from ingestion.worker import run_once
    from ops.watchdog import (
        WatchdogState,
        on_scan_failure,
        on_scan_success,
        run_watchdog,
    )
    from storage.database import SessionLocal

    engine_state.started_at = time.monotonic()
    engine_state.scan_interval_seconds = settings.scan_interval_seconds
    ensure_background_probe()
    iteration = 0
    _last_nc_run_monotonic: float = 0.0  # monotonic timestamp of last NC run
    _watchdog_state = WatchdogState()
    try:
        while not stop.is_set():
            iteration += 1
            try:
                engine_state.mark_scanning(
                    iteration=iteration, message=f"Scan iteration {iteration}"
                )
                result = run_once()
                engine_state.mark_scan_result(result)
                log.info("engine_scan_complete", iteration=iteration, result=result)
            except Exception as exc:  # noqa: BLE001 - individual scan failure must not kill the engine
                log.exception("engine_scan_failed", iteration=iteration, error=str(exc))
                engine_state.mark_error(str(exc))
                on_scan_failure(_watchdog_state)
            else:
                on_scan_success(_watchdog_state)

            # ── Watchdog: WAL checkpoint, VACUUM, disk, failure tracking ──
            # Runs every iteration regardless of scan outcome.
            try:
                with SessionLocal() as wd_session:
                    wd_result = run_watchdog(wd_session, _watchdog_state)
                    wd_session.commit()
                    if wd_result.get("degraded"):
                        log.warning("watchdog_degraded", result=wd_result)
            except Exception as exc:  # noqa: BLE001
                log.debug("watchdog_failed", error=str(exc))
            # ── Post-scan engine phases (each watchdog-guarded) ───────────────
            phase_error, _last_nc_run_monotonic = run_engine_phases(
                settings=settings,
                iteration=iteration,
                stop=stop,
                last_nc_run_monotonic=_last_nc_run_monotonic,
            )
            if not phase_error:
                engine_state.mark_completed()
            stop.wait(backoff_sleep_seconds(iteration, settings.scan_interval_seconds))
    finally:
        log.info("engine_shutting_down")
        # Clean up Night Crawler HTTP clients
        try:
            from crawlers.orchestrator import close_nightcrawler_orchestrator

            close_nightcrawler_orchestrator()
        except Exception:
            pass
        # Clean up LLM HTTP client
        try:
            from llm.engine import llm_engine

            llm_engine.close()
        except Exception:
            pass
        if ui.poll() is None:
            ui.terminate()
            try:
                ui.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ui.kill()
        api_server.should_exit = True
        api_thread.join(timeout=10)
        log.info("engine_stopped")


if __name__ == "__main__":
    main()
