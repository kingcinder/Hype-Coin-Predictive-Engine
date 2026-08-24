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
from pathlib import Path

from common.logging import get_logger

log = get_logger(__name__)


def _bootstrap() -> None:
    """Idempotent zero-container bootstrap: SQLite schema + reference rows + archive dir."""
    from common.config import get_settings
    from storage.database import Base, SessionLocal, engine
    from storage.seed import seed_reference_data

    settings = get_settings()
    Path(settings.archive_local_dir).mkdir(parents=True, exist_ok=True)
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


def main() -> None:
    _bootstrap()

    from common.config import get_settings

    settings = get_settings()

    stop = threading.Event()

    def _handle_stop(signum: int, _frame: object) -> None:
        log.info("engine_shutdown_signal", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    # --- REST API in a daemon thread -----------------------------------------
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
    from forecast.engine import maybe_run_forecast
    from ingestion.service import backoff_sleep_seconds
    from ingestion.source_clients import ensure_background_probe
    from ingestion.worker import run_once
    from ops.parity import maybe_run_parity
    from ops.retention import maybe_run_retention
    from storage.database import SessionLocal

    engine_state.started_at = time.monotonic()
    engine_state.scan_interval_seconds = settings.scan_interval_seconds
    ensure_background_probe()
    iteration = 0
    _last_nc_run_monotonic: float = 0.0  # monotonic timestamp of last NC run
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
                stop.wait(backoff_sleep_seconds(iteration, settings.scan_interval_seconds))
                continue
            phase_error = False
            try:
                engine_state.mark_forecasting()
                forecast = maybe_run_forecast()
                if forecast.get("status") != "skipped":
                    log.info("engine_forecast_training_complete", result=forecast)
            except Exception as exc:  # noqa: BLE001 - forecast failure must not kill the engine
                log.exception("engine_forecast_failed", iteration=iteration, error=str(exc))
                engine_state.mark_error(f"forecast: {exc}")
                phase_error = True
            try:
                engine_state.mark_retention()
                retention = maybe_run_retention()
                if not retention.get("skipped"):
                    log.info("engine_retention_complete", result=retention)
            except Exception as exc:  # noqa: BLE001 - retention failure must not kill the engine
                log.exception("engine_retention_failed", iteration=iteration, error=str(exc))
                engine_state.mark_error(f"retention: {exc}")
                phase_error = True
            # Lake-vs-SQL parity CI: daily comparison of the lake read path
            # against the live SQL path, paging a mismatch via ntfy.
            try:
                parity = maybe_run_parity()
                if not parity.get("skipped"):
                    log.info("engine_parity_complete", result=parity)
            except Exception as exc:  # noqa: BLE001 - parity failure must not kill the engine
                log.exception("engine_parity_failed", iteration=iteration, error=str(exc))
            # Night Crawler pass: crawl all sources, feed data lake
            # Gated by interval — only runs every nightcrawler_interval_minutes
            try:
                if settings.nightcrawler_enabled and not stop.is_set():
                    nc_interval_sec = settings.nightcrawler_interval_minutes * 60
                    now_mono = time.monotonic()
                    if (now_mono - _last_nc_run_monotonic) >= nc_interval_sec:
                        from crawlers.pipeline import run_nightcrawler_pipeline
                        with SessionLocal() as session:
                            nc_result = run_nightcrawler_pipeline(session)
                            session.commit()
                        _last_nc_run_monotonic = now_mono
                        log.info(
                            "engine_nightcrawler_complete",
                            iteration=iteration,
                            result=nc_result,
                        )
            except Exception as exc:  # noqa: BLE001
                log.exception("engine_nightcrawler_failed", iteration=iteration, error=str(exc))
                engine_state.mark_error(f"nightcrawler: {exc}")
                phase_error = True
            # Data lake pass: signal scoring + label densification + webhooks
            try:
                if settings.data_lake_enabled:
                    from data_lake.manager import run_data_lake_pass
                    with SessionLocal() as session:
                        dl_result = run_data_lake_pass(session)
                        session.commit()
                    log.info("engine_data_lake_complete", iteration=iteration, result=dl_result)
            except Exception as exc:  # noqa: BLE001 - data lake failure must not kill the engine
                log.exception("engine_data_lake_failed", iteration=iteration, error=str(exc))
                engine_state.mark_error(f"data_lake: {exc}")
                phase_error = True
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
