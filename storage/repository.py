from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from common.time import ensure_utc, utc_now
from storage import models


def stable_hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _json_safe(value: Any) -> Any:
    """Recursively convert non-JSON-native values to JSON-safe equivalents.

    Crawlers persist ``datetime`` objects (and occasionally sets/bytes) inside
    raw-evidence payloads; the JSON column serializer rejects them and rolls
    back the whole scan. Sanitizing here protects every caller regardless of
    which crawler produced the payload.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


def get_or_create_chain(
    session: Session, slug: str, *, name: str, vm_type: str, native_symbol: str
) -> models.Chain:
    row = session.scalar(select(models.Chain).where(models.Chain.slug == slug))
    if row:
        return row
    row = models.Chain(
        slug=slug,
        name=name,
        vm_type=vm_type,
        native_symbol=native_symbol,
        finality_profile={"mode": "provider_default"},
    )
    session.add(row)
    session.flush()
    return row


def get_or_create_source(
    session: Session, *, name: str, source_type: str, tier: str, base_url: str | None = None
) -> models.Source:
    row = session.scalar(select(models.Source).where(models.Source.name == name))
    if row:
        return row
    row = models.Source(name=name, source_type=source_type, tier=tier, base_url=base_url)
    session.add(row)
    session.flush()
    return row


def get_or_create_venue(
    session: Session,
    *,
    name: str,
    venue_type: str,
    chain_id: int | None,
    official_url: str | None = None,
) -> models.Venue:
    row = session.scalar(
        select(models.Venue).where(models.Venue.name == name, models.Venue.chain_id == chain_id)
    )
    if row:
        return row
    row = models.Venue(
        name=name, venue_type=venue_type, chain_id=chain_id, official_url=official_url
    )
    session.add(row)
    session.flush()
    return row


def upsert_asset(
    session: Session,
    *,
    chain_id: int,
    address: str,
    symbol: str,
    name: str | None,
    first_seen_at: datetime,
    website_url: str | None = None,
    github_url: str | None = None,
) -> models.Asset:
    row = session.scalar(
        select(models.Asset).where(
            models.Asset.chain_id == chain_id, models.Asset.address == address
        )
    )
    safe_symbol = _truncate(symbol or "UNKNOWN", 64) or "UNKNOWN"
    safe_name = _truncate(name, 256)
    safe_website = _truncate(website_url, 1024)
    safe_github = _truncate(github_url, 1024)
    if row:
        row.symbol = safe_symbol or row.symbol
        row.name = safe_name or row.name
        row.website_url = safe_website or row.website_url
        row.github_url = safe_github or row.github_url
        row.updated_at = utc_now()
        return row
    row = models.Asset(
        chain_id=chain_id,
        address=address,
        symbol=safe_symbol,
        name=safe_name,
        first_seen_at=first_seen_at,
        identity_confidence=0.3,
        website_url=safe_website,
        github_url=safe_github,
    )
    session.add(row)
    session.flush()
    return row


def upsert_contract(
    session: Session,
    *,
    chain_id: int,
    asset_id: int | None,
    address: str,
    observed_at: datetime,
    verified_flag: bool = False,
    proxy_flag: bool = False,
    deployer_wallet: str | None = None,
) -> models.Contract:
    row = session.scalar(
        select(models.Contract).where(
            models.Contract.chain_id == chain_id, models.Contract.address == address
        )
    )
    if row:
        row.asset_id = asset_id or row.asset_id
        row.verified_flag = row.verified_flag or verified_flag
        row.proxy_flag = row.proxy_flag or proxy_flag
        row.deployer_wallet = deployer_wallet or row.deployer_wallet
        row.observed_at = max(ensure_utc(row.observed_at), ensure_utc(observed_at))
        return row
    row = models.Contract(
        chain_id=chain_id,
        asset_id=asset_id,
        address=address,
        verified_flag=verified_flag,
        proxy_flag=proxy_flag,
        deployer_wallet=deployer_wallet,
        observed_at=observed_at,
    )
    session.add(row)
    session.flush()
    return row


def upsert_pool_and_pair(
    session: Session,
    *,
    chain_id: int,
    dex_id: str,
    pair_address: str,
    base_asset_id: int,
    quote_asset_id: int | None,
    created_at_source: datetime | None,
) -> tuple[models.Pool, models.Pair]:
    pool = session.scalar(
        select(models.Pool).where(
            models.Pool.chain_id == chain_id, models.Pool.address == pair_address
        )
    )
    if not pool:
        pool = models.Pool(
            chain_id=chain_id,
            address=pair_address,
            dex_id=dex_id,
            base_asset_id=base_asset_id,
            quote_asset_id=quote_asset_id,
            created_at_source=created_at_source,
        )
        session.add(pool)
        session.flush()
    venue = get_or_create_venue(session, name=dex_id, venue_type="dex", chain_id=chain_id)
    pair = session.scalar(
        select(models.Pair).where(
            models.Pair.venue_id == venue.id,
            models.Pair.base_asset_id == base_asset_id,
            models.Pair.quote_asset_id == quote_asset_id,
            models.Pair.pool_id == pool.id,
        )
    )
    if not pair:
        pair = models.Pair(
            venue_id=venue.id,
            chain_id=chain_id,
            base_asset_id=base_asset_id,
            quote_asset_id=quote_asset_id,
            pool_id=pool.id,
            created_at_source=created_at_source,
        )
        session.add(pair)
        session.flush()
    return pool, pair


def store_raw_evidence(
    session: Session,
    *,
    source: models.Source,
    payload: Any,
    observed_at: datetime,
    effective_at: datetime | None = None,
    raw_path: str | None = None,
) -> models.RawEvidenceItem:
    content_hash = stable_hash(payload)
    row = session.scalar(
        select(models.RawEvidenceItem).where(
            models.RawEvidenceItem.source_id == source.id,
            models.RawEvidenceItem.content_hash == content_hash,
        )
    )
    if row:
        return row
    row = models.RawEvidenceItem(
        source_id=source.id,
        source_type=source.source_type,
        source_tier=source.tier,
        observed_at=observed_at,
        effective_at=effective_at,
        raw_path=raw_path,
        content_hash=content_hash,
        payload=_json_safe(payload if isinstance(payload, dict) else {"items": payload}),
    )
    session.add(row)
    session.flush()
    return row


def insert_market_snapshot_once(
    session: Session,
    *,
    pair_id: int,
    source_id: int,
    ts: datetime,
    observed_at: datetime,
    price_usd: float | None,
    volume_usd: float | None,
    buys: int | None = None,
    sells: int | None = None,
    trades: int | None = None,
    raw_evidence_id: int | None = None,
) -> models.MarketSnapshot:
    row = session.scalar(
        select(models.MarketSnapshot).where(
            models.MarketSnapshot.pair_id == pair_id,
            models.MarketSnapshot.ts == ts,
            models.MarketSnapshot.source_id == source_id,
        )
    )
    if row:
        return row
    row = models.MarketSnapshot(
        pair_id=pair_id,
        source_id=source_id,
        ts=ts,
        observed_at=observed_at,
        open=price_usd,
        high=price_usd,
        low=price_usd,
        close=price_usd,
        price_usd=price_usd,
        volume_usd=volume_usd,
        buys=buys,
        sells=sells,
        trades=trades,
        raw_evidence_id=raw_evidence_id,
    )
    session.add(row)
    session.flush()
    return row


def insert_liquidity_snapshot_once(
    session: Session,
    *,
    pool_id: int,
    source_id: int,
    ts: datetime,
    observed_at: datetime,
    reserve_usd: float | None,
    reserve_base: float | None = None,
    reserve_quote: float | None = None,
    raw_evidence_id: int | None = None,
) -> models.LiquiditySnapshot:
    row = session.scalar(
        select(models.LiquiditySnapshot).where(
            models.LiquiditySnapshot.pool_id == pool_id,
            models.LiquiditySnapshot.ts == ts,
            models.LiquiditySnapshot.source_id == source_id,
        )
    )
    if row:
        return row
    row = models.LiquiditySnapshot(
        pool_id=pool_id,
        source_id=source_id,
        ts=ts,
        observed_at=observed_at,
        reserve_base=reserve_base,
        reserve_quote=reserve_quote,
        reserve_usd=reserve_usd,
        raw_evidence_id=raw_evidence_id,
    )
    session.add(row)
    session.flush()
    return row


def insert_holder_once(
    session: Session,
    *,
    asset_id: int,
    wallet_address: str,
    source_id: int,
    ts: datetime,
    observed_at: datetime,
    balance: float,
    pct_supply: float | None,
) -> models.Holder:
    row = session.scalar(
        select(models.Holder).where(
            models.Holder.asset_id == asset_id,
            models.Holder.wallet_address == wallet_address,
            models.Holder.ts == ts,
            models.Holder.source_id == source_id,
        )
    )
    if row:
        row.observed_at = max(ensure_utc(row.observed_at), ensure_utc(observed_at))
        row.balance = balance
        row.pct_supply = pct_supply
        return row
    row = models.Holder(
        asset_id=asset_id,
        wallet_address=wallet_address,
        source_id=source_id,
        ts=ts,
        observed_at=observed_at,
        balance=balance,
        pct_supply=pct_supply,
    )
    session.add(row)
    session.flush()
    return row


def upsert_feature(
    session: Session,
    *,
    asset_id: int,
    decision_ts: datetime,
    feature_name: str,
    feature_value: float,
    source_count: int,
    freshness_score: float,
    missing_flag: bool,
    source_refs: dict[str, Any] | None = None,
) -> models.Feature:
    row = session.scalar(
        select(models.Feature).where(
            models.Feature.asset_id == asset_id,
            models.Feature.decision_ts == decision_ts,
            models.Feature.feature_name == feature_name,
        )
    )
    if row:
        row.feature_value = feature_value
        row.source_count = source_count
        row.freshness_score = freshness_score
        row.missing_flag = missing_flag
        row.observed_at = utc_now()
        row.source_refs = source_refs or {}
        return row
    row = models.Feature(
        asset_id=asset_id,
        decision_ts=decision_ts,
        observed_at=utc_now(),
        feature_name=feature_name,
        feature_value=feature_value,
        source_count=source_count,
        freshness_score=freshness_score,
        missing_flag=missing_flag,
        source_refs=source_refs or {},
    )
    session.add(row)
    session.flush()
    return row


def upsert_social_mention(
    session: Session,
    *,
    asset_id: int | None,
    topic: str | None,
    source_id: int,
    ts: datetime,
    observed_at: datetime,
    author_hash: str | None = None,
    metrics_json: dict[str, Any] | None = None,
    raw_ref: str | None = None,
) -> models.SocialMention:
    ref = raw_ref or f"mention:{source_id}:{ts.isoformat()}:{stable_hash(topic or '')}"
    row = session.scalar(
        select(models.SocialMention).where(
            models.SocialMention.source_id == source_id,
            models.SocialMention.ts == ts,
            models.SocialMention.raw_ref == ref,
        )
    )
    if row:
        row.asset_id = asset_id or row.asset_id
        row.topic = topic or row.topic
        row.metrics_json = metrics_json or row.metrics_json
        return row
    row = models.SocialMention(
        asset_id=asset_id,
        topic=topic,
        source_id=source_id,
        ts=ts,
        observed_at=observed_at,
        author_hash=author_hash,
        metrics_json=metrics_json or {},
        raw_ref=ref,
    )
    session.add(row)
    session.flush()
    return row


def upsert_news_item(
    session: Session,
    *,
    source_id: int,
    published_at: datetime | None,
    observed_at: datetime,
    source_domain: str,
    title: str,
    url: str,
    official_flag: bool = False,
    raw_evidence_id: int | None = None,
) -> models.NewsItem:
    url_hash = stable_hash(url)
    row = session.scalar(select(models.NewsItem).where(models.NewsItem.url_hash == url_hash))
    if row:
        return row
    row = models.NewsItem(
        source_id=source_id,
        published_at=published_at,
        observed_at=observed_at,
        source_domain=source_domain[:256],
        title_hash=stable_hash(title),
        title=title,
        url_hash=url_hash,
        official_flag=official_flag,
        raw_evidence_id=raw_evidence_id,
    )
    session.add(row)
    session.flush()
    return row


def upsert_catalyst(
    session: Session,
    *,
    asset_id: int,
    catalyst_type: str,
    scheduled_at: datetime | None,
    published_at: datetime | None,
    observed_at: datetime,
    confidence: float,
    source_id: int,
) -> models.Catalyst:
    row = session.scalar(
        select(models.Catalyst).where(
            models.Catalyst.asset_id == asset_id,
            models.Catalyst.catalyst_type == catalyst_type,
            models.Catalyst.scheduled_at == scheduled_at,
            models.Catalyst.source_id == source_id,
        )
    )
    if row:
        return row
    row = models.Catalyst(
        asset_id=asset_id,
        catalyst_type=catalyst_type,
        scheduled_at=scheduled_at,
        published_at=published_at,
        observed_at=observed_at,
        confidence=confidence,
        source_id=source_id,
    )
    session.add(row)
    session.flush()
    return row


def upsert_prelaunch_candidate(
    session: Session,
    *,
    asset_id: int,
    decision_ts: datetime,
    priority_score: float,
    drivers: dict[str, Any],
    model_version: str,
) -> models.PrelaunchCandidate:
    row = session.scalar(
        select(models.PrelaunchCandidate).where(
            models.PrelaunchCandidate.asset_id == asset_id,
            models.PrelaunchCandidate.decision_ts == decision_ts,
            models.PrelaunchCandidate.model_version == model_version,
        )
    )
    if row:
        row.priority_score = priority_score
        row.drivers = drivers
        row.observed_at = utc_now()
        return row
    row = models.PrelaunchCandidate(
        asset_id=asset_id,
        decision_ts=decision_ts,
        observed_at=utc_now(),
        priority_score=priority_score,
        drivers=drivers,
        model_version=model_version,
    )
    session.add(row)
    session.flush()
    return row


def upsert_forecast(
    session: Session,
    *,
    asset_id: int,
    decision_ts: datetime,
    p_ignition_24h: float,
    p_collapse_24h: float,
    expected_hours_to_peak: float | None,
    expected_hours_to_collapse: float | None,
    calibration_bucket: str | None,
    calibrated: bool,
    details: dict[str, Any],
    model_version: str,
) -> models.Forecast:
    row = session.scalar(
        select(models.Forecast).where(
            models.Forecast.asset_id == asset_id,
            models.Forecast.decision_ts == decision_ts,
            models.Forecast.model_version == model_version,
        )
    )
    if row:
        row.p_ignition_24h = p_ignition_24h
        row.p_collapse_24h = p_collapse_24h
        row.expected_hours_to_peak = expected_hours_to_peak
        row.expected_hours_to_collapse = expected_hours_to_collapse
        row.calibration_bucket = calibration_bucket
        row.calibrated = calibrated
        row.details = details
        row.observed_at = utc_now()
        return row
    row = models.Forecast(
        asset_id=asset_id,
        decision_ts=decision_ts,
        observed_at=utc_now(),
        p_ignition_24h=p_ignition_24h,
        p_collapse_24h=p_collapse_24h,
        expected_hours_to_peak=expected_hours_to_peak,
        expected_hours_to_collapse=expected_hours_to_collapse,
        calibration_bucket=calibration_bucket,
        calibrated=calibrated,
        details=details,
        model_version=model_version,
    )
    session.add(row)
    session.flush()
    return row


def record_health(
    session: Session,
    *,
    component: str,
    state: str,
    message: str | None = None,
    freshness_sec: float | None = None,
    lag_sec: float | None = None,
    error_count: int = 0,
    ts: datetime | None = None,
) -> models.SystemHealth:
    row = models.SystemHealth(
        component=component,
        ts=ts or utc_now(),
        freshness_sec=freshness_sec,
        lag_sec=lag_sec,
        error_count=error_count,
        state=state,
        message=message,
    )
    session.add(row)
    session.flush()
    return row


def latest_features_for_asset(
    session: Session, asset_id: int, decision_ts: datetime
) -> dict[str, models.Feature]:
    rows = session.scalars(
        select(models.Feature).where(
            models.Feature.asset_id == asset_id,
            models.Feature.decision_ts == decision_ts,
        )
    ).all()
    return {row.feature_name: row for row in rows}


def latest_scores(
    session: Session,
    *,
    limit: int = 50,
    include_black: bool = False,
    order_by: str = "research_priority",
) -> list[models.Score]:
    latest_ts = session.scalar(select(func.max(models.Score.decision_ts)))
    if latest_ts is None:
        return []
    stmt = select(models.Score).where(models.Score.decision_ts == latest_ts)
    if not include_black:
        stmt = stmt.where(models.Score.risk_band != "BLACK")
    if order_by == "hype":
        stmt = stmt.order_by(desc(models.Score.hype))
    else:
        stmt = stmt.order_by(desc(models.Score.research_priority))
    return list(session.scalars(stmt.limit(limit)))


def record_scan_result(
    session: Session,
    *,
    ts: datetime,
    duration_sec: float | None = None,
    pairs: int = 0,
    profiles: int = 0,
    scores: int = 0,
    ignition_events: int = 0,
    fingerprints: int = 0,
    forecasts: int = 0,
    lifecycle: int = 0,
    narrative: int = 0,
    mempool: int = 0,
    lp_removals: int = 0,
    prelaunch: int = 0,
    catalysts: int = 0,
    archive: int = 0,
    ntfy_sent: int = 0,
    rpc_pool_notifications: int = 0,
    rpc_pool_snapshots: int = 0,
    state: str = "ok",
    error_message: str | None = None,
    details: dict[str, Any] | None = None,
) -> models.ScanResult:
    row = models.ScanResult(
        ts=ts,
        duration_sec=duration_sec,
        pairs=pairs,
        profiles=profiles,
        scores=scores,
        ignition_events=ignition_events,
        fingerprints=fingerprints,
        forecasts=forecasts,
        lifecycle=lifecycle,
        narrative=narrative,
        mempool=mempool,
        lp_removals=lp_removals,
        prelaunch=prelaunch,
        catalysts=catalysts,
        archive=archive,
        ntfy_sent=ntfy_sent,
        rpc_pool_notifications=rpc_pool_notifications,
        rpc_pool_snapshots=rpc_pool_snapshots,
        state=state,
        error_message=error_message,
        details=details or {},
    )
    session.add(row)
    session.flush()
    return row


def latest_scan_result(session: Session) -> models.ScanResult | None:
    return session.scalar(select(models.ScanResult).order_by(desc(models.ScanResult.ts)).limit(1))


def latest_health(session: Session, *, limit: int = 25) -> list[models.SystemHealth]:
    sub = (
        select(models.SystemHealth.component, func.max(models.SystemHealth.ts).label("max_ts"))
        .group_by(models.SystemHealth.component)
        .subquery()
    )
    stmt = (
        select(models.SystemHealth)
        .join(
            sub,
            and_(
                models.SystemHealth.component == sub.c.component,
                models.SystemHealth.ts == sub.c.max_ts,
            ),
        )
        .order_by(models.SystemHealth.component)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def latest_watchdog_timeouts(session: Session, *, limit: int = 20) -> list[models.SystemHealth]:
    """Recent engine phase-watchdog alarms (most recent first).

    These are the ``red`` rows recorded by ``engine/run.py`` when a blocking
    phase (retention / forecast / parity / nightcrawler / data-lake) exceeded
    its watchdog deadline and was abandoned. Identified by the message shape
    ``*watchdog timeout; pass abandoned, engine loop continuing``. Surfaced in
    Feed Health and the Archive & Retention view.
    """
    return list(
        session.scalars(
            select(models.SystemHealth)
            .where(models.SystemHealth.message.like("%watchdog timeout; pass abandoned%"))
            .order_by(desc(models.SystemHealth.ts))
            .limit(limit)
        )
    )


def assets_for_ids(session: Session, asset_ids: Iterable[int]) -> dict[int, models.Asset]:
    ids = list(asset_ids)
    if not ids:
        return {}
    return {
        asset.id: asset
        for asset in session.scalars(select(models.Asset).where(models.Asset.id.in_(ids))).all()
    }
