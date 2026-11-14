from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from common.config import get_settings
from trading.errors import RulesIncomplete


@dataclass(frozen=True)
class Rules:
    buy_trigger: str | None
    stop_pct: Decimal | None
    target_pct: Decimal | None
    time_stop_minutes: int | None
    sha256: str


def load_rules(path: str | None = None) -> Rules:
    p = Path(path or get_settings().trading_rules_path)
    raw = p.read_text() if p.exists() else "{}"
    data = json.loads(raw)
    sha = hashlib.sha256(raw.encode()).hexdigest()

    def _d(k: str) -> Decimal | None:
        v = data.get(k)
        return None if v in (None, "") else Decimal(str(v))

    ts = data.get("time_stop_minutes")
    trig = data.get("buy_trigger")
    return Rules(
        buy_trigger=trig.strip() if isinstance(trig, str) and trig.strip() else None,
        stop_pct=_d("stop_pct"),
        target_pct=_d("target_pct"),
        time_stop_minutes=int(ts) if ts not in (None, "") else None,
        sha256=sha,
    )


def validate_for_open(r: Rules) -> None:
    missing: list[str] = []
    if r.buy_trigger is None:
        missing.append("buy_trigger")
    if r.stop_pct is None:
        missing.append("stop_pct")
    if r.target_pct is None and r.time_stop_minutes is None:
        missing.append("target_pct_or_time_stop_minutes")
    if missing:
        raise RulesIncomplete(missing)
