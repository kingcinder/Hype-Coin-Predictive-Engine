"""Read-only score histogram diagnostic grouped by UTC decision day."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.config import get_settings  # noqa: E402
from storage.database import make_engine  # noqa: E402
from storage.models import Score  # noqa: E402


@dataclass(frozen=True)
class ScoreSample:
    decision_ts: datetime
    risk_score: float | None
    exit_risk: float | None
    uncertainty: float | None


_COLUMNS = ("risk_score", "exit_risk", "uncertainty")


def _number(value: float) -> str:
    return format(float(value), ".8g")


def summarize_score_rows(rows: Iterable[ScoreSample]) -> dict[str, dict[str, dict[str, object]]]:
    grouped: dict[str, list[ScoreSample]] = defaultdict(list)
    for row in rows:
        grouped[row.decision_ts.date().isoformat()].append(row)

    report: dict[str, dict[str, dict[str, object]]] = {}
    for day, samples in sorted(grouped.items()):
        report[day] = {}
        for column in _COLUMNS:
            values = [getattr(sample, column) for sample in samples]
            counts = Counter(_number(value) for value in values if value is not None)
            modes = sorted(counts.items(), key=lambda item: (-item[1], float(item[0])))[:5]
            report[day][column] = {
                "count": sum(counts.values()),
                "distinct_values": len(counts),
                "histogram": dict(sorted(counts.items(), key=lambda item: float(item[0]))),
                "top_modes": [{"value": value, "count": count} for value, count in modes],
            }
    return report


def _seeded_fixture_rows() -> list[ScoreSample]:
    """Deterministic local samples used only when no readable score DB exists."""
    return [
        ScoreSample(datetime(2026, 1, 1, hour), risk, exit_risk, uncertainty)
        for hour, (risk, exit_risk, uncertainty) in enumerate(
            [(20.0, 15.0, 10.0), (35.0, 28.0, 20.0), (50.0, 42.0, 35.0), (75.0, 68.0, 50.0)]
        )
    ]


def _load_database_rows(database_url: str) -> list[ScoreSample]:
    engine = make_engine(database_url=database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                select(Score.decision_ts, Score.risk, Score.exit_risk, Score.uncertainty).order_by(
                    Score.decision_ts
                )
            )
            return [
                ScoreSample(
                    decision_ts=row.decision_ts,
                    risk_score=row.risk,
                    exit_risk=row.exit_risk,
                    uncertainty=row.uncertainty,
                )
                for row in rows
            ]
    finally:
        engine.dispose()


def _database_is_present(database_url: str) -> bool:
    url = make_url(database_url)
    if url.drivername.startswith("sqlite"):
        path = Path(url.database or ":memory:")
        return path.exists() if str(path) != ":memory:" else False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="database path or SQLAlchemy URL")
    args = parser.parse_args(argv)
    database_url = args.db or get_settings().database_url
    if args.db and "://" not in database_url:
        database_url = f"sqlite+pysqlite:///{database_url}"

    try:
        rows = _load_database_rows(database_url) if _database_is_present(database_url) else []
    except Exception as exc:  # noqa: BLE001 - diagnostic falls back without modifying the DB.
        print(f"source=seeded-fixtures reason=database-unavailable ({type(exc).__name__})")
        rows = []
    source = "live-db" if rows else "seeded-fixtures"
    if not rows:
        rows = _seeded_fixture_rows()
    print(f"source={source} rows={len(rows)} read_only=true")
    print(json.dumps(summarize_score_rows(rows), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
