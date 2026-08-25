"""Shared API client helpers for Streamlit UI views.

Avoids circular imports: every view imports api_get/api_post from here
instead of from ui.app directly.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")


def api_get(path: str, *, params: dict[str, Any] | None = None) -> Any:
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(f"{API_BASE_URL}{path}", params=params)
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # noqa: BLE001 - visible operator state.
        st.error(f"API request failed: {path}: {exc}")
        return None


def api_post(path: str, *, json: dict[str, Any] | None = None) -> Any:
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{API_BASE_URL}{path}", json=json)
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # noqa: BLE001 - visible operator state.
        st.error(f"API request failed: {path}: {exc}")
        return None


def api_get_silent(path: str, *, params: dict[str, Any] | None = None, timeout: float = 3.0) -> Any:
    """Like :func:`api_get` but never renders UI on failure.

    For chrome elements that probe on every rerun (the live engine banner and
    the system resource strip) a failed probe must stay silent — the caller
    falls back to the SSE snapshot instead of flashing an error.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{API_BASE_URL}{path}", params=params)
            if response.status_code != 200:
                return None
            return response.json()
    except Exception:
        return None


def api_status(timeout: float = 2.0) -> tuple[str, str]:
    """Silent health probe for chrome elements (sidebar footer etc.).

    Returns ``(state, label)`` where state is one of ``ok``/``yellow``/
    ``down`` and label is a short human-readable status. Never raises and
    never renders UI — safe to call on every rerun.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{API_BASE_URL}/health")
            if response.status_code == 200:
                data = response.json()
                state = data.get("status", "ok")
                return (state, state.upper())
            return ("down", f"HTTP {response.status_code}")
    except Exception:
        return ("down", "OFFLINE")
