#!/usr/bin/env python3
"""Social-presence prior falsification — the Task 13 gate artifact.

Contingency table over historical launches: rows are Telegram presence
at first observation (``scoring.entry_rank``'s point-in-time flags) vs
not; columns are the pump label (Task 2's ``forward_max_gain_pct`` >=
1.0 at any decision observed by ``as_of``) vs not. The statistic is the
lift = P(pump | telegram) / P(pump | no telegram).

Exit code: 0 iff lift >= 2.0 with n(telegram-present) >= 30, else 1.
The report (JSON + MD) is written either way. **Demotion rule:** an
exit-1 verdict on the live window-C run demotes the prior —
``Settings.entry_social_prior_multiplier`` becomes 1.0 and presence
only breaks exact rank-sum ties (see ``scoring/entry_rank.py``).

Window discipline (plan Global Constraints): the flags are
point-in-time by construction, and this run is window C only — the
multiplier value is a prior chosen on window-B reasoning, never fitted
on the window this script measures.

Usage:
  python scripts/social_prior_falsification.py [--as-of <ISO-8601>]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from common.config import Settings, get_settings  # noqa: E402
from common.time import ensure_utc, utc_now  # noqa: E402
from forecast.labels import LABEL_FORWARD_MAX_GAIN  # noqa: E402
from scoring.entry_rank import social_presence_at_first_observation  # noqa: E402
from storage import models  # noqa: E402
from storage.database import session_scope  # noqa: E402

REPORTS_DIR = REPO / "reports"

# The falsification gate (plan Task 13): the prior survives only if
# Telegram presence at first observation at least doubles the pump
# rate, on a Telegram sample big enough to mean anything.
MIN_LIFT = 2.0
MIN_TELEGRAM_N = 30
# The §5 pump label: forward_max_gain_pct >= 1.0 (a doubling in-window).
PUMP_GAIN_THRESHOLD = 1.0


def _contingency(session: Session, *, as_of: datetime) -> dict[str, int]:
    """Count launches by Telegram presence x pumped, as of ``as_of``.

    A launch enters the table only once it carries a
    ``forward_max_gain_pct`` label observed by ``as_of`` (unmeasured
    launches are excluded, never counted as non-pumps — the mission's
    censoring discipline). An asset with several labels is judged by
    its best forward gain.
    """
    rows = session.execute(
        select(models.Label.asset_id, models.Label.label_value).where(
            models.Label.label_type == LABEL_FORWARD_MAX_GAIN,
            models.Label.observed_at <= as_of,
        )
    ).all()
    best_gain: dict[int, float] = {}
    for asset_id, raw_value in rows:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if value > best_gain.get(asset_id, float("-inf")):
            best_gain[asset_id] = value
    counts = {
        "telegram_present": 0,
        "telegram_pumped": 0,
        "no_telegram": 0,
        "no_telegram_pumped": 0,
    }
    for asset_id, gain in best_gain.items():
        presence = social_presence_at_first_observation(session, asset_id)
        pumped = gain >= PUMP_GAIN_THRESHOLD
        if presence.telegram:
            counts["telegram_present"] += 1
            counts["telegram_pumped"] += int(pumped)
        else:
            counts["no_telegram"] += 1
            counts["no_telegram_pumped"] += int(pumped)
    return counts


def _evaluate(counts: dict[str, int]) -> tuple[float | None, bool]:
    """Lift and gate verdict; lift is None when the base rate is 0/0."""
    telegram_n = counts["telegram_present"]
    plain_n = counts["no_telegram"]
    telegram_rate = counts["telegram_pumped"] / telegram_n if telegram_n else 0.0
    plain_rate = counts["no_telegram_pumped"] / plain_n if plain_n else 0.0
    lift = telegram_rate / plain_rate if plain_rate > 0 else None
    passed = lift is not None and lift >= MIN_LIFT and telegram_n >= MIN_TELEGRAM_N
    return lift, passed


def _render_markdown(payload: dict) -> str:
    lift = payload["lift"]
    return "\n".join(
        [
            f"# Social-presence prior falsification — {payload['as_of'][:10]}",
            "",
            f"- as_of: {payload['as_of']}",
            f"- pump label: forward_max_gain_pct >= {payload['pump_gain_threshold']}",
            f"- telegram-present launches: {payload['telegram_present']} "
            f"(pumped: {payload['telegram_pumped']})",
            f"- no-telegram launches: {payload['no_telegram']} "
            f"(pumped: {payload['no_telegram_pumped']})",
            f"- lift: {lift:.4f}" if lift is not None else "- lift: undefined (base rate 0)",
            f"- gate: lift >= {payload['min_lift']} with n(telegram) >= "
            f"{payload['min_telegram_n']} -> {'PASS' if payload['passed'] else 'FAIL'}",
            f"- entry_social_prior_multiplier judged: {payload['entry_social_prior_multiplier']}",
            "",
            "FAIL demotes the prior: the multiplier default becomes 1.0 and",
            "presence only breaks exact rank-sum ties (plan Task 13).",
            "",
        ]
    )


def run_falsification(
    *,
    session=None,
    settings: Settings | None = None,
    as_of: datetime | None = None,
    reports_dir: Path | None = None,
) -> int:
    """Write the falsification report; return the gate exit code.

    ``session`` is an injectable seam (keyword-only): when None — the
    CLI path — this call opens and owns its own ``session_scope()``
    cycle.
    """
    settings = settings if settings is not None else get_settings()
    as_of = ensure_utc(as_of) if as_of is not None else utc_now()
    out_dir = reports_dir if reports_dir is not None else REPORTS_DIR

    if session is not None:
        counts = _contingency(session, as_of=as_of)
    else:
        with session_scope() as active:
            counts = _contingency(active, as_of=as_of)

    lift, passed = _evaluate(counts)
    payload = {
        "as_of": as_of.isoformat(),
        "pump_gain_threshold": PUMP_GAIN_THRESHOLD,
        "min_lift": MIN_LIFT,
        "min_telegram_n": MIN_TELEGRAM_N,
        "entry_social_prior_multiplier": settings.entry_social_prior_multiplier,
        **counts,
        "lift": lift,
        "passed": passed,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = as_of.strftime("%Y%m%d")
    json_path = out_dir / f"social-prior-falsification-{stamp}.json"
    md_path = out_dir / f"social-prior-falsification-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    print(
        f"social prior: telegram {counts['telegram_pumped']}/{counts['telegram_present']} "
        f"pumped vs no-telegram {counts['no_telegram_pumped']}/{counts['no_telegram']} "
        f"-> lift {lift if lift is not None else 'undefined'} "
        f"({'PASS' if passed else 'FAIL'}); wrote {json_path} + {md_path}"
    )
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Social-presence prior contingency falsification (Task 13 gate)"
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 timestamp to measure as of (default: now, UTC)",
    )
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    return run_falsification(as_of=as_of)


if __name__ == "__main__":
    sys.exit(main())
