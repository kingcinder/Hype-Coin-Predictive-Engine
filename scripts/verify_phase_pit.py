"""Verify stored ``lifecycle_phase`` feature values are point-in-time correct.

Recomputes the phase from lifecycle events (``ts <= decision`` AND
``observed_at <= decision``, latest by ``ts``) — the exact rule in
``features/factory.py::_lifecycle_phase`` — and compares it against the
stored feature value.  Reports matches, mismatches, rows with no
determining event, and point-in-time violations (feature ``observed_at``
after its ``decision_ts``).

Read-only: opens the DB in URI read-only mode and never writes.

Usage:
    .venv/bin/python scripts/verify_phase_pit.py --db serpent.db.v3-lab-2026-11-20
    .venv/bin/python scripts/verify_phase_pit.py --db <path> --out reports/phase-pit-check.json
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

PHASE_RANK = {
    "seeding": 0,
    "ignition": 1,
    "parabolic": 2,
    "saturation": 3,
    "collapse": 4,
    # dead / rugged / survivor / anything unrecognised ranks 5, mirroring
    # pump_physics.engine.phase_rank's default.
}


def _rank(phase: str) -> int:
    return PHASE_RANK.get(phase, 5)


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to engine DB (read-only)")
    parser.add_argument("--out", default="", help="optional JSON report path")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()

    # Lifecycle events are few (~4k): load them all, grouped per asset,
    # sorted by ts.  Each entry is (ts, observed_at, rank).
    events: dict[int, list[tuple[datetime, datetime, int]]] = {}
    event_ts: dict[int, list[datetime]] = {}
    for asset_id, ts, observed_at, phase in cur.execute(
        "SELECT asset_id, ts, observed_at, phase FROM lifecycle_events ORDER BY asset_id, ts"
    ):
        events.setdefault(asset_id, []).append((_parse(ts), _parse(observed_at), _rank(phase)))
    for asset_id, rows in events.items():
        event_ts[asset_id] = [row[0] for row in rows]

    stats = {
        "checked": 0,
        "matched": 0,
        "mismatched": 0,
        "no_determining_event": 0,
        "violations": 0,
        "mismatch_examples": [],
        "no_event_examples": [],
    }
    for asset_id, decision_ts, feature_value, observed_at in cur.execute(
        "SELECT asset_id, decision_ts, feature_value, observed_at FROM features "
        "WHERE feature_name = 'lifecycle_phase' AND missing_flag = 0 "
        "ORDER BY asset_id, decision_ts"
    ):
        decision = _parse(decision_ts)
        observed = _parse(observed_at)
        stats["checked"] += 1
        if observed is not None and decision is not None and observed > decision:
            stats["violations"] += 1
        rows = events.get(asset_id, [])
        # Latest event by ts with ts <= decision AND observed_at <= decision:
        # take the ts<=decision prefix, scan it backwards for the first row
        # whose observed_at is also <= decision.
        determining = None
        if rows:
            idx = bisect.bisect_right(event_ts[asset_id], decision) - 1
            while idx >= 0:
                if rows[idx][1] is not None and rows[idx][1] <= decision:
                    determining = rows[idx]
                    break
                idx -= 1
        if determining is None:
            stats["no_determining_event"] += 1
            if len(stats["no_event_examples"]) < 5:
                stats["no_event_examples"].append(
                    {
                        "asset_id": asset_id,
                        "decision_ts": decision_ts,
                        "feature_value": feature_value,
                    }
                )
            continue
        if abs(determining[2] - float(feature_value)) < 1e-9:
            stats["matched"] += 1
        else:
            stats["mismatched"] += 1
            if len(stats["mismatch_examples"]) < 5:
                stats["mismatch_examples"].append(
                    {
                        "asset_id": asset_id,
                        "decision_ts": decision_ts,
                        "feature_value": feature_value,
                        "recomputed": determining[2],
                        "event_ts": determining[0].isoformat(),
                        "event_observed_at": determining[1].isoformat(),
                    }
                )
    con.close()

    total = stats["checked"] or 1
    print(
        f"checked={stats['checked']} matched={stats['matched']} "
        f"({100.0 * stats['matched'] / total:.2f}%) "
        f"mismatched={stats['mismatched']} "
        f"no_determining_event={stats['no_determining_event']} "
        f"violations_observed_after_decision={stats['violations']}"
    )
    for example in stats["mismatch_examples"]:
        print("MISMATCH:", example)
    for example in stats["no_event_examples"]:
        print("NO-EVENT:", example)
    if args.out:
        Path(args.out).write_text(json.dumps(stats, indent=2), encoding="utf-8")
        print(f"WROTE: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
