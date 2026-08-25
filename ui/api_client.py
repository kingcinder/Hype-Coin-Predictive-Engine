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
