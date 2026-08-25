from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import AlertState, AlertType
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health, upsert_catalyst

log = get_logger(__name__)

_CATALYST_RULES: list[tuple[str, re.Pattern[str], float]] = [
    ("tge", re.compile(r"\b(tge|token generation event)\b", re.IGNORECASE), 0.7),
    ("presale", re.compile(r"\b(presale|pre-sale|pre sale)\b", re.IGNORECASE), 0.6),
    ("airdrop", re.compile(r"\bairdrop\b", re.IGNORECASE), 0.7),
    ("unlock", re.compile(r"\b(unlock|token unlock|vesting)\b", re.IGNORECASE), 0.6),
    ("listing", re.compile(r"\b(listing|list on|goes live on)\b", re.IGNORECASE), 0.6),
    ("launch", re.compile(r"\b(launch|mainnet launch|launches)\b", re.IGNORECASE), 0.5),
    ("audit", re.compile(r"\baudit\b", re.IGNORECASE), 0.4),
]

_DATE_PATTERNS = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(
        r"\b(\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4})\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(tomorrow)\b", re.IGNORECASE),
]


def extract_catalyst_type(text: str) -> tuple[str | None, float]:
    for catalyst_type, pattern, confidence in _CATALYST_RULES:
        if pattern.search(text):
            return catalyst_type, confidence
    return None, 0.0


def extract_scheduled_at(text: str, observed_at: datetime) -> datetime | None:
    observed_at = ensure_utc(observed_at)
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).lower()
        if raw == "tomorrow":
            return (observed_at + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=UTC)
        except ValueError:
            try:
                parsed = datetime.strptime(raw, "%d %b %Y").replace(tzinfo=UTC)
                return parsed
            except ValueError:
                continue
    return None


def extract_catalysts(session: Session, *, decision_ts: datetime | None = None) -> int:
    decision_ts = ensure_utc(decision_ts or utc_now())
    created = 0
    try:
        news_rows = session.scalars(
            select(models.NewsItem).where(models.NewsItem.observed_at <= decision_ts)
        ).all()
        for news in news_rows:
            catalyst_type, confidence = extract_catalyst_type(news.title)
            if not catalyst_type:
                continue
            asset = _resolve_asset_for_title(session, news.title)
            if not asset:
                continue
            scheduled_at = extract_scheduled_at(news.title, news.observed_at or decision_ts)
            existing = session.scalar(
                select(models.Catalyst).where(
                    models.Catalyst.asset_id == asset.id,
                    models.Catalyst.catalyst_type == catalyst_type,
                    models.Catalyst.scheduled_at == scheduled_at,
                    models.Catalyst.source_id == news.source_id,
                )
            )
            if existing:
                continue
            upsert_catalyst(
                session,
                asset_id=asset.id,
                catalyst_type=catalyst_type,
                scheduled_at=scheduled_at,
                published_at=news.published_at or news.observed_at,
                observed_at=decision_ts,
                confidence=confidence,
                source_id=news.source_id,
            )
            created += 1
        record_health(
            session,
            component="catalyst_timetable",
            state="ok",
            message=f"{created} catalysts extracted",
        )
    except Exception as exc:  # noqa: BLE001 - preserve exact extraction failure.
        log.exception("catalyst_extract_failed", error=str(exc))
        record_health(
            session,
            component="catalyst_timetable",
            state="red",
            message=str(exc),
            error_count=1,
        )
    return created


def _resolve_asset_for_title(session: Session, title: str) -> models.Asset | None:
    lowered = title.lower()
    for asset in session.scalars(select(models.Asset)).all():
        symbol = asset.symbol.lower()
        if symbol and len(symbol) >= 2 and symbol in lowered:
            return asset
    return None


def alert_upcoming_catalysts(session: Session, *, decision_ts: datetime | None = None) -> int:
    """Raise UPCOMING_CATALYST alerts for catalysts scheduled within the window."""
    settings = get_settings()
    decision_ts = ensure_utc(decision_ts or utc_now())
    window_end = decision_ts + timedelta(hours=settings.catalyst_alert_hours)
    rows = session.scalars(
        select(models.Catalyst).where(
            models.Catalyst.scheduled_at >= decision_ts,
            models.Catalyst.scheduled_at <= window_end,
            models.Catalyst.observed_at <= decision_ts,
        )
    ).all()
    created = 0
    for catalyst in rows:
        asset = session.get(models.Asset, catalyst.asset_id)
        if not asset:
            continue
        ref = f"catalyst:{catalyst.id}"
        existing = session.scalar(
            select(models.Alert).where(
                models.Alert.asset_id == asset.id,
                models.Alert.alert_type == AlertType.UPCOMING_CATALYST.value,
                models.Alert.score_snapshot_ref == ref,
            )
        )
        if existing:
            continue
        scheduled_at = catalyst.scheduled_at or decision_ts
        hours = max(
            0.0,
            (ensure_utc(scheduled_at) - decision_ts).total_seconds() / 3600.0,
        )
        from ops.alert_quality import alert_generation_allowed

        if not alert_generation_allowed(session, AlertType.UPCOMING_CATALYST.value, settings):
            continue
        session.add(
            models.Alert(
                asset_id=asset.id,
                alert_type=AlertType.UPCOMING_CATALYST.value,
                threshold_version=settings.model_version,
                score_snapshot_ref=ref,
                state=AlertState.OPEN.value,
                message=(
                    f"{asset.symbol} {catalyst.catalyst_type} scheduled in "
                    f"{hours:.0f}h (confidence {catalyst.confidence:.0%})."
                ),
            )
        )
        session.flush()
        created += 1
    return created
