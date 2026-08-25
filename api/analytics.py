from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from features.definitions import FEATURE_NAMES
from storage import models


@dataclass(frozen=True)
class SimilarSetup:
    asset_id: int
    decision_ts: datetime
    similarity_score: float
    distance: float
    features_compared: int
    score: models.Score | None


def feature_vector(session: Session, *, asset_id: int, decision_ts: datetime) -> dict[str, float]:
    rows = session.scalars(
        select(models.Feature).where(
            models.Feature.asset_id == asset_id,
            models.Feature.decision_ts == decision_ts,
            models.Feature.missing_flag.is_(False),
        )
    ).all()
    return {row.feature_name: float(row.feature_value) for row in rows}


def _normalized_distance(
    target: dict[str, float], candidate: dict[str, float]
) -> tuple[float, int]:
    squared: list[float] = []
    for name in FEATURE_NAMES:
        if name not in target or name not in candidate:
            continue
        left = target[name]
        right = candidate[name]
        scale = max(1.0, abs(left), abs(right))
        squared.append(((left - right) / scale) ** 2)
    if not squared:
        return 1.0, 0
    return min(1.0, math.sqrt(sum(squared) / len(squared))), len(squared)


def similar_setups(
    session: Session,
    *,
    asset_id: int,
    decision_ts: datetime,
    limit: int = 10,
    min_features: int = 6,
) -> list[SimilarSetup]:
    target = feature_vector(session, asset_id=asset_id, decision_ts=decision_ts)
    if not target:
        return []

    feature_rows = session.scalars(
        select(models.Feature).where(
            models.Feature.decision_ts <= decision_ts,
            models.Feature.missing_flag.is_(False),
            ~((models.Feature.asset_id == asset_id) & (models.Feature.decision_ts == decision_ts)),
        )
    ).all()

    candidates: dict[tuple[int, datetime], dict[str, float]] = defaultdict(dict)
    for row in feature_rows:
        candidates[(row.asset_id, row.decision_ts)][row.feature_name] = float(row.feature_value)

    output: list[SimilarSetup] = []
    for (candidate_asset_id, candidate_ts), candidate in candidates.items():
        distance, compared = _normalized_distance(target, candidate)
        if compared < min_features:
            continue
        output.append(
            SimilarSetup(
                asset_id=int(candidate_asset_id),
                decision_ts=candidate_ts,
                similarity_score=round(100.0 * (1.0 - distance), 4),
                distance=round(distance, 6),
                features_compared=compared,
                score=None,
            )
        )

    ranked: list[SimilarSetup] = []
    seen_assets: set[int] = set()
    for item in sorted(
        output,
        key=lambda item: (item.similarity_score, item.features_compared),
        reverse=True,
    ):
        if item.asset_id in seen_assets:
            continue
        seen_assets.add(item.asset_id)
        ranked.append(item)
        if len(ranked) >= limit:
            break
    for idx, item in enumerate(ranked):
        score = session.scalar(
            select(models.Score)
            .where(
                models.Score.asset_id == item.asset_id,
                models.Score.decision_ts == item.decision_ts,
            )
            .order_by(models.Score.model_version)
            .limit(1)
        )
        ranked[idx] = SimilarSetup(
            asset_id=item.asset_id,
            decision_ts=item.decision_ts,
            similarity_score=item.similarity_score,
            distance=item.distance,
            features_compared=item.features_compared,
            score=score,
        )
    return ranked
