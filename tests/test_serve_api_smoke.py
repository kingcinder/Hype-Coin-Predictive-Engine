"""Smoke-check ``python scripts/serve_api.py`` from the repository root."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from storage import models  # noqa: F401 - populate Base.metadata for the smoke DB.
from storage.database import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
API_TOKEN = "serve-api-smoke-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> tuple[int, object]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_TOKEN}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5.0) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _wait_for_health(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> dict:
    deadline = time.monotonic() + 30.0
    last_error: str | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            status, body = _get_json(f"{base_url}/health")
            if status == 200:
                assert isinstance(body, dict)
                return body
            last_error = f"HTTP {status}"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    pytest.fail(
        f"serve_api did not become healthy (poll={proc.poll()}, last_error={last_error}); "
        f"log:\n{log}"
    )


def test_serve_api_direct_entrypoint_serves_health_and_rpc_pool(tmp_path: Path) -> None:
    """Run the script without PYTHONPATH, then exercise both smoke endpoints."""
    db_path = tmp_path / "serve-api.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "API_HOST": "127.0.0.1",
        "API_PORT": str(port),
        "SERPENT_DB_PATH": str(db_path),
        "ENGINE_API_TOKEN": API_TOKEN,
        "ENGINE_API_AUTH_BYPASS": "false",
        "RPC_POOL_BACKGROUND_PROBE_ENABLED": "false",
    }
    # The point of this check: direct execution must not need an import path
    # supplied by the caller.
    env.pop("PYTHONPATH", None)

    log_path = tmp_path / "serve-api.log"
    with log_path.open("wb") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "scripts/serve_api.py"],
            cwd=REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            health = _wait_for_health(proc, base_url, log_path)
            assert health["database"] == "ok"

            status, pools = _get_json(f"{base_url}/rpc/pool")
            assert status == 200
            assert isinstance(pools, list)
            assert pools, "expected at least one RPC pool chain"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
