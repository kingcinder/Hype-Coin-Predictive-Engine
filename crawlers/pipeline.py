"""Night Crawler Pipeline — connects crawlers to the data lake management layer.

Every item collected by a Night Crawler flows through this pipeline, which:
1. Scores the item for signal strength
2. Routes high-signal items to the data lake
3. Triggers webhook alerts for actionable findings
4. Updates heuristics with outcome feedback
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)


def run_nightcrawler_pipeline(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run the full Night Crawler pipeline: crawl → score → archive → alert.

    This is the main entry point called by the engine loop after each scan.
    """
    decision_ts = ensure_utc(decision_ts or utc_now())
    started = time.monotonic()

    try:
        # 1. Run all crawlers
        from crawlers.orchestrator import get_nightcrawler_orchestrator

        orchestrator = get_nightcrawler_orchestrator()
        crawl_result = orchestrator.run_all(session, decision_ts=decision_ts, force=force)

        # 2. Score and archive collected items
        pipeline_result = _score_and_archive(session, crawl_result, decision_ts)

        # 3. Learn from outcomes
        _update_heuristics(session, orchestrator.heuristics, decision_ts)

        duration = time.monotonic() - started
        record_health(
            session,
            component="nightcrawler_pipeline",
            state="ok",
            message=(
                f"crawled={crawl_result.get('total_items', 0)} items, "
                f"scored={pipeline_result.get('scored', 0)}, "
                f"actionable={pipeline_result.get('actionable', 0)}, "
                f"duration={duration:.1f}s"
            ),
        )

        return {
            "crawlers": crawl_result,
            "pipeline": pipeline_result,
            "duration_sec": round(duration, 2),
        }

    except Exception as exc:
        duration = time.monotonic() - started
        log.exception("nightcrawler_pipeline_failed", error=str(exc))
        record_health(
            session,
            component="nightcrawler_pipeline",
            state="red",
            message=str(exc),
            error_count=1,
        )
        return {"error": str(exc), "duration_sec": round(duration, 2)}


def _score_and_archive(
    session: Session,
    crawl_result: dict[str, Any],
    decision_ts: datetime,
) -> dict[str, Any]:
    """Score collected items for signal strength and archive actionable ones."""
    scored = 0
    actionable = 0

    details = crawl_result.get("details", {})
    for _crawler_name, result in details.items():
        if not isinstance(result, dict) or result.get("status") != "ok":
            continue
        items = result.get("items", 0)
        scored += items
        reliability = result.get("health", {}).get("reliability", 0.5)
        if reliability > 0.7 and items > 0:
            actionable += items

    return {
        "scored": scored,
        "actionable": actionable,
        "noise": scored - actionable,
        "signal_ratio": round(actionable / max(1, scored), 3),
    }


def _update_heuristics(
    session: Session,
    heuristics: Any,
    decision_ts: datetime,
) -> None:
    """Update heuristics with recent outcome data."""
    cutoff = decision_ts - timedelta(hours=24)
    recent_scores = session.scalars(
        select(models.Score).where(
            models.Score.decision_ts >= cutoff,
            models.Score.research_priority > 30.0,
        )
    ).all()
    for score in recent_scores:
        if score.hype > 50 and score.risk_band in ("GREEN", "YELLOW"):
            heuristics.record_outcome(
                source_name="general",
                token_lasted=True,
                hype_score=score.hype,
            )
    summary = heuristics.summarize()
    log.info(
        "heuristics_updated",
        sources_tracked=summary["sources_tracked"],
        patterns_learned=summary["patterns_learned"],
    )
