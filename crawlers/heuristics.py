"""Heuristics Engine — self-adjusting intelligence for the Night Crawlers.

Learns which data sources provide the most actionable signals, adjusts
crawl frequencies based on source reliability, tracks pattern correlations
with successful hype coins, and prunes low-value sources automatically.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.logging import get_logger
from common.time import utc_now
from storage import models

log = get_logger(__name__)


@dataclass
class SourceReliability:
    """Tracks reliability and signal quality of a data source."""

    source_name: str
    total_dispatches: int = 0
    actionable_dispatches: int = 0
    avg_signal_score: float = 0.0
    last_actionable_at: datetime | None = None
    effective_frequency_multiplier: float = 1.0

    @property
    def actionability_rate(self) -> float:
        if self.total_dispatches == 0:
            return 0.5
        return self.actionable_dispatches / self.total_dispatches

    @property
    def recommendation(self) -> str:
        if self.actionability_rate > 0.3:
            return "increase"
        elif self.actionability_rate < 0.05:
            return "decrease"
        return "maintain"


@dataclass
class PatternMemory:
    """Remembers which patterns correlate with successful hype coins."""

    pattern_key: str
    occurrence_count: int = 0
    success_count: int = 0  # led to tokens that lasted >24h
    avg_hype_at_detection: float = 0.0
    avg_time_to_peak_hours: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.occurrence_count == 0:
            return 0.0
        return self.success_count / self.occurrence_count

    @property
    def confidence(self) -> float:
        """Confidence increases with more data points."""
        return min(1.0, math.log1p(self.occurrence_count) / math.log1p(100))


class HeuristicsEngine:
    """Self-adjusting heuristics that learn from outcomes.

    Tracks source reliability, pattern correlations, and adjusts
    crawl frequencies to maximize signal-to-noise ratio over time.
    """

    def __init__(self) -> None:
        self._source_reliabilities: dict[str, SourceReliability] = {}
        self._pattern_memory: dict[str, PatternMemory] = {}

    def analyze_source_reliability(
        self, session: Session, source_name: str
    ) -> SourceReliability:
        """Analyze how reliable and actionable a source has been."""
        # Count signals from this source in the last 7 days
        cutoff = utc_now() - timedelta(days=7)

        total_signals = session.scalar(
            select(func.count()).select_from(models.SocialMention).where(
                models.SocialMention.source_id.in_(
                    select(models.Source.id).where(models.Source.name == source_name)
                ),
                models.SocialMention.observed_at >= cutoff,
            )
        ) or 0

        # Count signals that led to scored tokens
        actionable = session.scalar(
            select(func.count()).select_from(models.Score).where(
                models.Score.decision_ts >= cutoff,
                models.Score.research_priority > 20.0,
            )
        ) or 0

        reliability = SourceReliability(
            source_name=source_name,
            total_dispatches=total_signals,
            actionable_dispatches=min(actionable, total_signals),
        )
        self._source_reliabilities[source_name] = reliability
        return reliability

    def record_outcome(
        self,
        source_name: str,
        token_lasted: bool,
        hype_score: float,
        time_to_peak_hours: float | None = None,
    ) -> None:
        """Record the outcome of a detection from a source."""
        if source_name not in self._source_reliabilities:
            self._source_reliabilities[source_name] = SourceReliability(
                source_name=source_name
            )
        rel = self._source_reliabilities[source_name]
        if token_lasted:
            rel.actionable_dispatches += 1
            rel.last_actionable_at = utc_now()

    def learn_pattern(
        self,
        pattern_key: str,
        success: bool,
        hype_at_detection: float = 0.0,
    ) -> None:
        """Learn from a pattern observation."""
        if pattern_key not in self._pattern_memory:
            self._pattern_memory[pattern_key] = PatternMemory(
                pattern_key=pattern_key
            )
        pm = self._pattern_memory[pattern_key]
        pm.occurrence_count += 1
        if success:
            pm.success_count += 1
        # Exponential moving average
        alpha = 0.2
        pm.avg_hype_at_detection = (
            alpha * hype_at_detection + (1 - alpha) * pm.avg_hype_at_detection
        )

    def get_crawl_frequency_multiplier(self, source_name: str) -> float:
        """Get the adaptive frequency multiplier for a source.

        Returns >1.0 for sources that should be crawled more often,
        <1.0 for sources that should be crawled less often.
        """
        rel = self._source_reliabilities.get(source_name)
        if rel is None:
            return 1.0

        if rel.actionability_rate > 0.3:
            return min(2.0, 1.0 + rel.actionability_rate)
        elif rel.actionability_rate < 0.05:
            return max(0.25, 0.5 - rel.actionability_rate)
        return 1.0

    def get_source_reliability(self, source_name: str) -> SourceReliability:
        return self._source_reliabilities.get(source_name, SourceReliability(source_name=source_name))

    def get_top_patterns(
        self, limit: int = 20
    ) -> list[PatternMemory]:
        """Get the most successful patterns for hype coin prediction."""
        patterns = sorted(
            self._pattern_memory.values(),
            key=lambda p: p.success_rate * p.confidence,
            reverse=True,
        )
        return patterns[:limit]

    def summarize(self) -> dict[str, Any]:
        """Summarize the heuristics engine state."""
        return {
            "sources_tracked": len(self._source_reliabilities),
            "patterns_learned": len(self._pattern_memory),
            "source_reliabilities": {
                name: {
                    "actionability_rate": round(rel.actionability_rate, 3),
                    "recommendation": rel.recommendation,
                    "frequency_multiplier": round(
                        self.get_crawl_frequency_multiplier(name), 2
                    ),
                }
                for name, rel in self._source_reliabilities.items()
            },
            "top_patterns": [
                {
                    "key": pm.pattern_key,
                    "success_rate": round(pm.success_rate, 3),
                    "confidence": round(pm.confidence, 3),
                    "occurrences": pm.occurrence_count,
                }
                for pm in self.get_top_patterns(10)
            ],
        }
