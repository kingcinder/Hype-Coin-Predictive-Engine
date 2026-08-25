"""Maintain the README coverage trend chart.

Reads ``coverage.xml`` (produced by ``pytest --cov --cov-report=xml``), appends
the run's total line-rate to ``coverage/history.json`` (newest first, capped),
and renders ``coverage/trend.svg`` — a compact line chart the README embeds so
coverage history is visible without opening Codecov.

Used by the ``coverage-history`` GitHub Actions workflow.  Safe to run locally:

    python -m pytest tests/ -q --cov=. --cov-report=xml
    python scripts/coverage_history.py

Exit codes: 0 success, 1 malformed input, 2 coverage dropped >10pp vs the
previous run (surfaces regressions in CI before merge).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

COVERAGE_DIR = Path(__file__).resolve().parents[1] / "coverage"
HISTORY_PATH = COVERAGE_DIR / "history.json"
TREND_PATH = COVERAGE_DIR / "trend.svg"
XML_PATH = Path("coverage.xml")
MAX_POINTS = 52  # one per weekly run ≈ one year of history


def _read_coverage_pct() -> float:
    """Extract the total line-rate from a coverage.py XML report."""
    if not XML_PATH.exists():
        raise SystemExit(f"missing {XML_PATH} — run pytest with --cov-report=xml first")
    root = ElementTree.parse(XML_PATH).getroot()
    # coverage 7.x: total is on the <coverage> root or a <summary> element.
    raw = root.get("line-rate")
    if raw is None:
        summary = root.find("summary")
        raw = summary.get("line-rate") if summary is not None else None
    if raw is None:
        raise SystemExit("coverage.xml has no line-rate — is it a coverage report?")
    return float(raw) * 100.0


def _load_history() -> list[dict[str, object]]:
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _render_svg(points: list[tuple[str, float]]) -> str:
    """Render a minimal self-contained SVG sparkline/trend chart."""
    width, height, pad = 520, 140, 24
    latest_value = points[-1][1] if points else 0.0
    # A single point has no line to draw — duplicate it at a slightly earlier x
    # so the geometry renders, while the real value is kept for the label.
    if len(points) == 1:
        date, value = points[0]
        points = [(date, value), (date, value)]
    elif not points:
        points = [(datetime.now(UTC).isoformat(), 0.0), (datetime.now(UTC).isoformat(), 100.0)]
    values = [p[1] for p in points]
    low, high = min(values), max(values)
    span = max(high - low, 1.0)
    low_pad = max(low - span * 0.2, 0.0)
    high_pad = min(high + span * 0.2, 100.0)
    range_ = max(high_pad - low_pad, 1.0)

    def xy(index: int, value: float) -> tuple[float, float]:
        x = pad + index * (width - 2 * pad) / max(len(points) - 1, 1)
        y = height - pad - (value - low_pad) / range_ * (height - 2 * pad)
        return round(x, 1), round(y, 1)

    path = "M " + " L ".join(f"{x},{y}" for x, y in (xy(i, v) for i, (_, v) in enumerate(points)))
    area = path + f" L {width - pad},{height - pad} L {pad},{height - pad} Z"
    last_x, last_y = xy(len(points) - 1, values[-1])

    ticks = "".join(
        f'<text x="{pad}" y="{pad + i * (height - 2 * pad) / 4}" font-size="9" '
        f'fill="#8890a0">{int(high_pad - i * (high_pad - low_pad) / 4)}%</text>'
        for i in range(5)
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#1a1f2e"/>
  <path d="{area}" fill="rgba(0,212,255,0.12)" stroke="none"/>
  <path d="{path}" fill="none" stroke="#00d4ff" stroke-width="2"/>
  <circle cx="{last_x}" cy="{last_y}" r="3.5" fill="#00ff88"/>
  <text x="{last_x - 30}" y="{max(last_y - 8, 12)}" font-size="11" font-weight="700" fill="#00ff88">{latest_value:.1f}%</text>
  {ticks}
</svg>
"""


def main() -> int:
    pct = _read_coverage_pct()
    history = _load_history()
    previous = history[0].get("pct") if history else None
    history.insert(
        0,
        {
            "date": datetime.now(UTC).date().isoformat(),
            "pct": round(pct, 2),
        },
    )
    history = history[:MAX_POINTS]

    COVERAGE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=2) + "\n")
    points = [(str(h.get("date", "")), float(h.get("pct", 0))) for h in reversed(history)]
    TREND_PATH.write_text(_render_svg(points))

    if previous is not None and pct < previous - 10.0:
        print(f"coverage dropped {previous - pct:.1f}pp since last run", file=sys.stderr)
        return 2
    print(f"coverage {pct:.2f}% recorded -> {HISTORY_PATH.name}, {TREND_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
