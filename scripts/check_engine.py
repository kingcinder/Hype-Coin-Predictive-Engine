#!/usr/bin/env python3
"""Verify the local engine process, ports, and authenticated API endpoints."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_PORT = 8000
UI_PORT = 8501
API_BASE = f"http://127.0.0.1:{API_PORT}"
ENGINE_PATTERN = re.compile(r"(?:^|/)python[0-9.]*\s+-m\s+engine(?:\s|$)")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _engine_pids() -> list[int]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        capture_output=True,
        text=True,
        check=True,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        pid_text, command = fields
        if ENGINE_PATTERN.search(command):
            pids.append(int(pid_text))
    return pids


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.0):
            return True
    except OSError:
        return False


def _api_token() -> str:
    token = os.environ.get("ENGINE_API_TOKEN", "").strip()
    if token:
        return token
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "ENGINE_API_TOKEN":
            return value.strip().strip("\"'").strip()
    return ""


def _http_status(path: str, token: str) -> tuple[int | None, str | None]:
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5.0) as response:
            return response.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except OSError as exc:
        return None, str(exc)


def main() -> int:
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if ok:
            print(f"PASS: {label}{f' ({detail})' if detail else ''}")
        else:
            failures += 1
            print(f"FAIL: {label}{f' ({detail})' if detail else ''}")

    try:
        pids = _engine_pids()
        check(
            len(pids) == 1,
            "exactly one engine process",
            f"pid={pids[0]}" if len(pids) == 1 else f"found={pids}",
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        check(False, "exactly one engine process", f"process scan failed: {exc}")

    check(_port_listening(API_PORT), f"port {API_PORT} is listening")
    check(_port_listening(UI_PORT), f"port {UI_PORT} is listening")

    token = _api_token()
    if not token:
        check(False, "authenticated API checks", "ENGINE_API_TOKEN not set in env or .env")
        return 1

    for path in ("/health", "/paper/ledger"):
        status, error = _http_status(path, token)
        check(
            status == 200,
            f"GET {path} returns 200",
            f"status={status}" if status is not None else (error or "request failed"),
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
