"""Tracked-wallet config loader (Phase F2)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common.logging import get_logger

log = get_logger(__name__)

WALLETS_PATH = Path("config/tracked_wallets.json")
_SOLANA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_CACHE: tuple[Path, float, list[dict[str, Any]]] | None = None


def _valid(entry: dict[str, Any]) -> bool:
    address = str(entry.get("address") or "").strip()
    if not address:
        return False
    chain = str(entry.get("chain") or "solana").lower()
    return chain != "solana" or bool(_SOLANA_RE.fullmatch(address))


def load_wallets(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load tracked wallets, re-reading only when the file mtime changes."""
    global _CACHE
    target = Path(path) if path is not None else WALLETS_PATH
    if not target.exists():
        _CACHE = None
        return []
    mtime = target.stat().st_mtime
    if _CACHE is not None and _CACHE[0] == target and _CACHE[1] == mtime:
        return _CACHE[2]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("tracked_wallets_unreadable")
        return []
    raw = data.get("wallets") if isinstance(data, dict) else None
    entries: list[dict[str, Any]] = []
    for entry in raw or []:
        if not isinstance(entry, dict) or not _valid(entry):
            log.warning("tracked_wallet_invalid")
            continue
        entries.append(
            {
                "address": str(entry["address"]).strip(),
                "label": str(entry.get("label") or ""),
                "chain": str(entry.get("chain") or "solana").lower(),
            }
        )
    _CACHE = (target, mtime, entries)
    return entries
