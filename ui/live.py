"""Live engine-state subscriber for the Streamlit GUI.

Replaces the deprecated ``st.components.v1.html`` SSE/WS bridges (which
injected iframes that wrote into the browser's localStorage but were never
consumed by any view) with a server-side daemon thread that maintains a
persistent connection to the engine's ``/engine/stream`` SSE endpoint and
caches the latest snapshot in memory.

Every view reads from that cache through :func:`engine_snapshot`, so the
whole GUI updates the instant the engine phase changes — no iframe, no
localStorage round-trip, no polling against the API, and the connection
survives Streamlit reruns (the thread lives in the UI process, not in a
page render).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx

from ui.api_client import API_BASE_URL

_engine_state: dict[str, Any] = {}
_lock = threading.Lock()
_started = False


def _sse_worker() -> None:
    """Reconnect loop: hold the SSE stream open, cache every data payload."""
    retry = 1.0
    while True:
        try:
            with httpx.Client(timeout=None) as client:
                with client.stream("GET", f"{API_BASE_URL}/engine/stream") as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"SSE HTTP {response.status_code}")
                    retry = 1.0
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload:
                            continue
                        try:
                            data = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        with _lock:
                            _engine_state.clear()
                            _engine_state.update(data)
        except Exception:  # noqa: BLE001 - reconnect with backoff, never die.
            time.sleep(min(retry, 30.0))
            retry *= 2


def start_engine_subscriber() -> None:
    """Start the daemon SSE subscriber exactly once per UI process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_sse_worker, name="ui-engine-sse", daemon=True).start()


def engine_snapshot() -> dict[str, Any]:
    """Latest engine snapshot from the SSE stream ({} when not connected yet)."""
    with _lock:
        return dict(_engine_state)
