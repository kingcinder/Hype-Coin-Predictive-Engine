from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from api.analytics import similar_setups
from api.schemas import (
    AlertAckRequest,
    AlertQualityLedger,
    AlertQualityRow,
    AlertQualityTrendResponse,
    AlertQualityTrendRow,
    AlertRow,
    AlertSnoozeRequest,
    ArchiveManifestRow,
    BacktestRequest,
    BacktestResultRow,
    CatalystRow,
    EngineScanProgressRow,
    EngineStatusResponse,
    EnsembleStateRow,
    FeatureRow,
    FingerprintRow,
    ForecastRow,
    FusionSignalRow,
    HealthComponent,
    HealthResponse,
    IgnitionEventRow,
    LakeBudgetHealthRow,
    LifecycleEventRow,
    LifecycleTransitionAlertRow,
    NarrativeClusterRow,
    NotifierHealthRow,
    OpsConsoleResponse,
    ParityLatestResponse,
    ParityMismatchRow,
    PrelaunchRow,
    RetentionGrowthRow,
    RetentionRunRow,
    RiskCalibrationRow,
    RiskResponse,
    RpcPoolChainRow,
    RpcPoolEndpointRow,
    RpcPoolProbeRow,
    ScanResultRow,
    ScorerAccuracyRow,
    SeedResponse,
    SimilarSetupRow,
    TokenDetail,
    TokenScoreRow,
    TriggerResponse,
    VelocityFeatureRow,
)
from common.config import get_settings
from common.enums import AlertState
from common.logging import get_logger
from common.time import utc_now
from engine.activity_stream import activity_stream_broker, compute_activity_signal_score
from engine.price_stream import price_stream_broker
from engine.state import engine_state, sse_broker
from ingestion.rpc_pool import HEALTH_START, POOL_CHAINS, get_rpc_pool
from ops.parity import latest_parity
from risk_engine.rules import assess_risk, mask_unreliable_forecast
from storage import models
from storage.database import get_session
from storage.repository import (
    latest_health,
    latest_scan_result,
    latest_scores,
    latest_watchdog_timeouts,
)

log = get_logger(__name__)

app = FastAPI(
    title="Serpent Circle Hype-Coin Predictive Engine",
    version="0.1.0",
    description="Research-only crypto intelligence API with separated hype and risk scores.",
)

# The Streamlit GUI (typically :8501) calls this API cross-origin from the
# browser for both httpx JSON calls and the SSE live-status bridge. Without
# CORS the browser silently kills the EventSource connection, which breaks
# the Engine Control real-time panel. Local origins only — this is a
# single-user desktop tool, not a public API.
_CORS_ORIGINS = [
    "http://localhost:8501",
    "http://127.0.0.1:8501",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

DbSession = Annotated[Session, Depends(get_session)]


def _score_row(session: Session, score: models.Score) -> TokenScoreRow:
    asset = session.get(models.Asset, score.asset_id)
    chain = session.get(models.Chain, asset.chain_id) if asset else None
    return TokenScoreRow(
        asset_id=score.asset_id,
        chain=chain.slug if chain else "unknown",
        address=asset.address if asset else "",
        symbol=asset.symbol if asset else "UNKNOWN",
        name=asset.name if asset else None,
        decision_ts=score.decision_ts,
        hype=score.hype,
        ethos=score.ethos,
        risk=score.risk,
        liquidity_access=score.liquidity_access,
        manipulation=score.manipulation,
        confidence=score.confidence,
        uncertainty=score.uncertainty,
        catalyst=score.catalyst,
        exit_risk=score.exit_risk,
        research_priority=score.research_priority,
        risk_band=score.risk_band,
    )


@app.get("/health", response_model=HealthResponse)
def health(session: DbSession) -> HealthResponse:
    database = "ok"
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        log.warning("health_database_failed", error=str(exc))
        database = f"error: {exc}"
    components = [
        HealthComponent(
            component=row.component,
            state=row.state,
            ts=row.ts,
            message=row.message,
            freshness_sec=row.freshness_sec,
            lag_sec=row.lag_sec,
            error_count=row.error_count,
        )
        for row in latest_health(session)
    ]
    status = (
        "ok"
        if database == "ok" and all(c.state in {"ok", "yellow"} for c in components)
        else "degraded"
    )
    return HealthResponse(status=status, database=database, components=components)


@app.get("/watchdog/alarms", response_model=list[HealthComponent])
def watchdog_alarms(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[HealthComponent]:
    """Recent engine phase-watchdog alarms (most recent first).

    These red ``system_health`` rows are recorded when a blocking engine phase
    (retention / forecast / parity / nightcrawler / data-lake) exceeded its
    watchdog deadline and was abandoned in the background. Surfaced in the
    Feed Health and Archive & Retention views so a wedged phase is immediately
    visible to an operator; empty when no phase has ever timed out.
    """
    return [
        HealthComponent(
            component=row.component,
            state=row.state,
            ts=row.ts,
            message=row.message,
            freshness_sec=row.freshness_sec,
            lag_sec=row.lag_sec,
            error_count=row.error_count,
        )
        for row in latest_watchdog_timeouts(session, limit=limit)
    ]


@app.get("/parity/latest", response_model=ParityLatestResponse)
def parity_latest(session: DbSession) -> ParityLatestResponse | JSONResponse:
    """Structured summary of the most recent lake-vs-SQL parity run.

    Reads the latest ``component:parity`` health row, so the API/UI process can
    show the last run's mismatch count, decision window, and state without
    re-running the expensive comparison. 404 when no parity pass has run yet.
    """
    latest = latest_parity(session)
    if latest is None:
        return JSONResponse(status_code=404, content={"detail": "no parity run yet"})
    return ParityLatestResponse(**latest)


@app.get("/parity/mismatches", response_model=list[ParityMismatchRow])
def parity_mismatches(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    asset_id: int | None = None,
    feature: str | None = None,
) -> list[ParityMismatchRow]:
    """Reviewable divergence history: every recorded lake-vs-SQL mismatch,
    newest run first, optionally filtered by asset or feature."""
    stmt = (
        select(models.ParityMismatch)
        .order_by(
            models.ParityMismatch.run_ts.desc(),
            models.ParityMismatch.decision_ts.desc(),
        )
        .limit(limit)
    )
    if asset_id is not None:
        stmt = stmt.where(models.ParityMismatch.asset_id == asset_id)
    if feature:
        stmt = stmt.where(models.ParityMismatch.feature_name == feature)
    return [
        ParityMismatchRow(
            run_ts=row.run_ts,
            decision_ts=row.decision_ts,
            asset_id=row.asset_id,
            symbol=row.symbol,
            feature_name=row.feature_name,
            sql_value=row.sql_value,
            lake_value=row.lake_value,
            sql_missing=row.sql_missing,
            lake_missing=row.lake_missing,
            state=row.state,
        )
        for row in session.scalars(stmt).all()
    ]


@app.get("/tokens/hot", response_model=list[TokenScoreRow])
def tokens_hot(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    include_black: bool = False,
) -> list[TokenScoreRow]:
    rows = latest_scores(session, limit=limit, include_black=include_black, order_by="hype")
    return [_score_row(session, row) for row in rows]


@app.get("/scores/top", response_model=list[TokenScoreRow])
def scores_top(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    include_black: bool = False,
) -> list[TokenScoreRow]:
    rows = latest_scores(
        session, limit=limit, include_black=include_black, order_by="research_priority"
    )
    return [_score_row(session, row) for row in rows]


@app.get("/tokens/{asset_id}", response_model=TokenDetail)
def token_detail(asset_id: int, session: DbSession) -> TokenDetail:
    asset = session.get(models.Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    chain = session.get(models.Chain, asset.chain_id)
    score = session.scalar(
        select(models.Score)
        .where(models.Score.asset_id == asset_id)
        .order_by(desc(models.Score.decision_ts))
        .limit(1)
    )
    features: list[FeatureRow] = []
    explanation = None
    if score:
        features = [
            FeatureRow(
                name=row.feature_name,
                value=row.feature_value,
                missing=row.missing_flag,
                freshness_score=row.freshness_score,
                source_count=row.source_count,
            )
            for row in session.scalars(
                select(models.Feature)
                .where(
                    models.Feature.asset_id == asset_id,
                    models.Feature.decision_ts == score.decision_ts,
                )
                .order_by(models.Feature.feature_name)
            )
        ]
        exp = session.scalar(
            select(models.ScoreExplanation).where(models.ScoreExplanation.score_id == score.id)
        )
        if exp:
            explanation = {
                "drivers": exp.drivers,
                "risk_reasons": exp.risk_reasons,
                "missing_features": exp.missing_features,
                "changed_features": exp.changed_features,
            }
    return TokenDetail(
        asset_id=asset.id,
        chain=chain.slug if chain else "unknown",
        address=asset.address,
        symbol=asset.symbol,
        name=asset.name,
        status=asset.status,
        website_url=asset.website_url,
        github_url=asset.github_url,
        latest_score=_score_row(session, score) if score else None,
        features=features,
        explanation=explanation,
    )


def _alert_row(session: DbSession, row: models.Alert) -> AlertRow:
    asset = session.get(models.Asset, row.asset_id)
    return AlertRow(
        id=row.id,
        asset_id=row.asset_id,
        symbol=asset.symbol if asset else None,
        created_at=row.created_at,
        alert_type=row.alert_type,
        state=row.state,
        message=row.message,
        notified_at=row.notified_at,
        acked_at=row.acked_at,
        ack_quality=row.ack_quality,
        snoozed_until=row.snoozed_until,
    )


@app.get("/alerts", response_model=list[AlertRow])
def alerts(session: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[AlertRow]:
    rows = session.scalars(
        select(models.Alert).order_by(desc(models.Alert.created_at)).limit(limit)
    ).all()
    return [_alert_row(session, row) for row in rows]


@app.post("/alerts/{alert_id}/snooze", response_model=AlertRow)
def snooze_alert(
    alert_id: int,
    payload: AlertSnoozeRequest,
    session: DbSession,
) -> AlertRow:
    """Temporarily suppress an alert; it returns to open when the snooze expires."""
    alert = session.get(models.Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert not found: {alert_id}")
    alert.state = AlertState.OPEN.value
    alert.snoozed_until = utc_now() + timedelta(hours=payload.hours)
    session.commit()
    session.refresh(alert)
    return _alert_row(session, alert)


@app.post("/alerts/{alert_id}/ack", response_model=AlertRow)
def ack_alert(
    alert_id: int,
    payload: AlertAckRequest,
    session: DbSession,
) -> AlertRow:
    """ACK an open alert, optionally rating its signal quality.

    An ACKed alert leaves the notifier's open set (``state=open``), so repeat
    pushes are suppressed — the event-ref dedup means it is not re-created on
    later scans either. ``quality`` (``useful``/``noise``) feeds the
    signal-quality ledger; re-acking updates the rating."""
    alert = session.get(models.Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert not found: {alert_id}")
    quality = payload.quality.strip().lower() if payload.quality else None
    if quality not in (None, "useful", "noise"):
        raise HTTPException(
            status_code=422,
            detail="quality must be 'useful', 'noise', or empty",
        )
    now = utc_now()
    alert.state = AlertState.ACKED.value
    alert.acked_at = now
    alert.ack_quality = quality
    session.commit()
    session.refresh(alert)
    return _alert_row(session, alert)


@app.post("/alerts/types/{alert_type}/reenable", response_model=dict)
def reenable_alert_type(alert_type: str, session: DbSession) -> dict[str, object]:
    """Explicitly resume an alert family quieted by the quality gate."""
    from ops.alert_quality import reenable_alert_type as _reenable

    control = _reenable(session, alert_type)
    return {"alert_type": control.alert_type, "reenabled": control.reenabled}


@app.get("/alerts/quality", response_model=AlertQualityLedger)
def alert_quality_ledger(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AlertQualityLedger:
    """Signal-quality ledger: how often acked alerts were rated useful."""
    acked = session.scalars(
        select(models.Alert)
        .where(models.Alert.acked_at.is_not(None))
        .order_by(desc(models.Alert.acked_at))
    ).all()
    useful = sum(1 for row in acked if row.ack_quality == "useful")
    noise = sum(1 for row in acked if row.ack_quality == "noise")
    unrated = len(acked) - useful - noise
    rated = useful + noise
    useful_rate = (useful / rated) if rated else None
    recent: list[AlertQualityRow] = []
    for row in acked[:limit]:
        asset = session.get(models.Asset, row.asset_id)
        recent.append(
            AlertQualityRow(
                id=row.id,
                asset_id=row.asset_id,
                symbol=asset.symbol if asset else None,
                alert_type=row.alert_type,
                state=row.state,
                message=row.message,
                created_at=row.created_at,
                acked_at=row.acked_at,
                ack_quality=row.ack_quality,
            )
        )
    return AlertQualityLedger(
        total_acked=len(acked),
        useful=useful,
        noise=noise,
        unrated=unrated,
        useful_rate=useful_rate,
        recent=recent,
    )


@app.get("/alerts/quality/trend", response_model=AlertQualityTrendResponse)
def alert_quality_trend(
    session: DbSession,
    weeks: Annotated[int, Query(ge=1, le=104)] = 26,
) -> AlertQualityTrendResponse:
    """Weekly ACK signal quality by alert type.

    ``useful_rate`` is useful/(useful+noise); unrated ACKs remain visible in
    the bucket counts but do not distort the rate. SQLite and PostgreSQL use
    different date-truncation functions, so bucketing is intentionally done
    in Python after fetching the bounded ACK history.
    """
    cutoff = utc_now() - timedelta(weeks=weeks)
    rows = session.scalars(
        select(models.Alert)
        .where(
            models.Alert.acked_at.is_not(None),
            models.Alert.acked_at >= cutoff,
        )
        .order_by(models.Alert.acked_at)
    ).all()
    buckets: dict[tuple[date, str], dict[str, int]] = {}
    for row in rows:
        assert row.acked_at is not None
        acked_at = row.acked_at
        if acked_at.tzinfo is None:
            acked_at = acked_at.replace(tzinfo=UTC)
        day = acked_at.astimezone(UTC).date()
        week_start = day - timedelta(days=day.weekday())
        key = (week_start, row.alert_type)
        bucket = buckets.setdefault(key, {"useful": 0, "noise": 0, "unrated": 0})
        quality = row.ack_quality
        if quality == "useful":
            bucket["useful"] += 1
        elif quality == "noise":
            bucket["noise"] += 1
        else:
            bucket["unrated"] += 1
    output: list[AlertQualityTrendRow] = []
    for (week_start, alert_type), bucket in sorted(buckets.items()):
        week_date = week_start
        rated = bucket["useful"] + bucket["noise"]
        output.append(
            AlertQualityTrendRow(
                week_start=datetime.combine(week_date, dt_time.min, tzinfo=UTC),
                alert_type=alert_type,
                useful=bucket["useful"],
                noise=bucket["noise"],
                unrated=bucket["unrated"],
                total_acked=sum(bucket.values()),
                useful_rate=(bucket["useful"] / rated) if rated else None,
            )
        )
    return AlertQualityTrendResponse(weeks=output)


@app.get("/risk/calibration", response_model=RiskCalibrationRow)
def risk_calibration(session: DbSession) -> RiskCalibrationRow:
    """Current adaptive risk calibration: rule thresholds + ML band boundaries.

    The ML probability boundaries (yellow/orange/red) are learned directly
    from ML scorer outcomes; BLACK is structurally fixed at 0.75 and never
    calibrated.  When no calibration has run yet, defaults are returned so
    the GUI can show what the engine is currently using.
    """
    from risk_engine.calibrator import _get_active_calibration

    cal = _get_active_calibration(session)
    if cal is None:
        return RiskCalibrationRow()
    return RiskCalibrationRow(
        version=cal.version,
        calibrated_at=cal.calibrated_at,
        sample_size=cal.sample_size,
        yellow_threshold=cal.yellow_threshold,
        orange_threshold=cal.orange_threshold,
        red_threshold=cal.red_threshold,
        ml_yellow_threshold=cal.ml_yellow_threshold,
        ml_orange_threshold=cal.ml_orange_threshold,
        ml_red_threshold=cal.ml_red_threshold,
        band_precisions=cal.band_precisions,
        ml_band_precisions=cal.ml_band_precisions,
    )


@app.get("/ensemble/state", response_model=EnsembleStateRow)
def ensemble_state(session: DbSession) -> EnsembleStateRow:
    """Persisted adaptive ensemble state for the observability dashboard.

    Returns the current rule/ml/heuristic weights, per-scorer accuracy
    ledgers (with confidence calibration buckets), and the weight-history
    series so the GUI can chart weight evolution over time.  Empty/defaults
    when no scoring has persisted state yet.
    """
    state = session.scalar(select(models.EnsembleState).limit(1))
    if state is None:
        return EnsembleStateRow()

    # Calibration buckets live at ``state.calibration_buckets[scorer_name]``
    # (persisted as ``{bucket: [count, correct]}`` by the ensemble engine);
    # per-scorer accuracy rows expose them for the reliability diagram.
    bucket_map = state.calibration_buckets or {}
    scorers: list[ScorerAccuracyRow] = []
    for name, acc in sorted((state.scorer_accuracy or {}).items()):
        correct = float(acc.get("correct_predictions", 0) or 0)
        total = float(acc.get("total_predictions", 0) or 0)
        scorers.append(
            ScorerAccuracyRow(
                scorer_name=name,
                correct_predictions=correct,
                total_predictions=total,
                accuracy=correct / total if total else 0.5,
                calibration_buckets=bucket_map.get(name) or {},
            )
        )
    return EnsembleStateRow(
        current_weights=state.current_weights or {},
        scorer_accuracy=scorers,
        weight_history=list(state.weight_history or [])[-100:],
        calibration_buckets=bucket_map,
        total_predictions=state.total_predictions or 0,
        last_recalibrated_at=state.last_recalibrated_at,
        updated_at=state.updated_at,
    )


@app.get("/fusion/recent", response_model=list[FusionSignalRow])
def fusion_recent(session: DbSession, limit: int = 50) -> list[FusionSignalRow]:
    """Most recent cross-source fusion results per asset.

    Rows record how many independent crawler sources corroborated each
    asset and the fused confidence boost applied.  The GUI charts fusion
    activity per asset in the ensemble observability section.
    """
    rows = session.scalars(
        select(models.CrossSourceSignal)
        .order_by(desc(models.CrossSourceSignal.observed_at))
        .limit(limit)
    ).all()
    # Batch-load assets once instead of one query per row (N+1).
    asset_ids = {row.asset_id for row in rows}
    symbols: dict[int, str | None] = {}
    if asset_ids:
        symbols = {
            asset_id: symbol
            for asset_id, symbol in session.execute(
                select(models.Asset.id, models.Asset.symbol).where(models.Asset.id.in_(asset_ids))
            ).all()
        }
    out: list[FusionSignalRow] = []
    for row in rows:
        out.append(
            FusionSignalRow(
                asset_id=row.asset_id,
                symbol=symbols.get(row.asset_id),
                source_count=row.source_count,
                sources=list(row.sources or []),
                fusion_score=row.fusion_score,
                confidence_boost=row.confidence_boost,
                signal_agreement=row.signal_agreement,
                observed_at=row.observed_at,
            )
        )
    return out


@app.get("/risk/{asset_id}", response_model=RiskResponse)
def risk(asset_id: int, session: DbSession) -> RiskResponse:
    asset = session.get(models.Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    latest_ts = session.scalar(
        select(func.max(models.Feature.decision_ts)).where(models.Feature.asset_id == asset_id)
    )
    if not latest_ts:
        return RiskResponse(
            asset_id=asset_id,
            risk_band="YELLOW",
            risk_score=25,
            reasons=["No feature snapshot exists yet; risk is unknown, not safe."],
            hard_reject=False,
        )
    features = {
        row.feature_name: row.feature_value
        for row in session.scalars(
            select(models.Feature).where(
                models.Feature.asset_id == asset_id,
                models.Feature.decision_ts == latest_ts,
            )
        )
    }
    features, _ = mask_unreliable_forecast(session, features)
    assessment = assess_risk(features)
    return RiskResponse(
        asset_id=asset_id,
        risk_band=assessment.band.value,
        risk_score=assessment.score,
        reasons=assessment.reasons,
        hard_reject=assessment.hard_reject,
    )


@app.get("/tokens/{asset_id}/similar", response_model=list[SimilarSetupRow])
def token_similar_setups(
    asset_id: int,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    min_features: Annotated[int, Query(ge=1, le=20)] = 6,
) -> list[SimilarSetupRow]:
    asset = session.get(models.Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    latest_ts = session.scalar(
        select(func.max(models.Feature.decision_ts)).where(models.Feature.asset_id == asset_id)
    )
    if latest_ts is None:
        return []
    rows = similar_setups(
        session,
        asset_id=asset_id,
        decision_ts=latest_ts,
        limit=limit,
        min_features=min_features,
    )
    out: list[SimilarSetupRow] = []
    for row in rows:
        candidate = session.get(models.Asset, row.asset_id)
        chain = session.get(models.Chain, candidate.chain_id) if candidate else None
        out.append(
            SimilarSetupRow(
                asset_id=row.asset_id,
                chain=chain.slug if chain else "unknown",
                address=candidate.address if candidate else "",
                symbol=candidate.symbol if candidate else "UNKNOWN",
                name=candidate.name if candidate else None,
                decision_ts=row.decision_ts,
                similarity_score=row.similarity_score,
                distance=row.distance,
                features_compared=row.features_compared,
                hype=row.score.hype if row.score else None,
                risk_band=row.score.risk_band if row.score else None,
                research_priority=row.score.research_priority if row.score else None,
            )
        )
    return out


def _ignition_event_row(session: Session, row: models.IgnitionEvent) -> IgnitionEventRow:
    asset = session.get(models.Asset, row.asset_id)
    chain = session.get(models.Chain, asset.chain_id) if asset else None
    return IgnitionEventRow(
        id=row.id,
        asset_id=row.asset_id,
        symbol=asset.symbol if asset else None,
        chain=chain.slug if chain else "unknown",
        event_type=row.event_type,
        ts=row.ts,
        observed_at=row.observed_at,
        confidence=row.confidence,
        details=row.details,
    )


def _fingerprint_row(session: Session, row: models.FingerprintAssessment) -> FingerprintRow:
    asset = session.get(models.Asset, row.asset_id)
    chain = session.get(models.Chain, asset.chain_id) if asset else None
    return FingerprintRow(
        id=row.id,
        asset_id=row.asset_id,
        symbol=asset.symbol if asset else None,
        chain=chain.slug if chain else "unknown",
        decision_ts=row.decision_ts,
        recidivism_score=row.recidivism_score,
        matched_cluster_count=row.matched_cluster_count,
        matched_wallet_count=row.matched_wallet_count,
        matched_roles=row.matched_roles,
        matched_clusters=row.matched_clusters,
    )


@app.get("/radar/ignitions", response_model=list[IgnitionEventRow])
def radar_ignitions(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    event_type: str | None = None,
) -> list[IgnitionEventRow]:
    stmt = select(models.IgnitionEvent).order_by(desc(models.IgnitionEvent.ts)).limit(limit)
    if event_type:
        stmt = stmt.where(models.IgnitionEvent.event_type == event_type)
    rows = session.scalars(stmt).all()
    return [_ignition_event_row(session, row) for row in rows]


@app.get("/fingerprint/top", response_model=list[FingerprintRow])
def fingerprint_top(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[FingerprintRow]:
    latest_ts = session.scalar(select(func.max(models.FingerprintAssessment.decision_ts)))
    if latest_ts is None:
        return []
    rows = session.scalars(
        select(models.FingerprintAssessment)
        .where(models.FingerprintAssessment.decision_ts == latest_ts)
        .order_by(desc(models.FingerprintAssessment.recidivism_score))
        .limit(limit)
    ).all()
    return [_fingerprint_row(session, row) for row in rows]


@app.get("/fingerprint/{asset_id}", response_model=FingerprintRow)
def fingerprint_detail(asset_id: int, session: DbSession) -> FingerprintRow:
    row = session.scalar(
        select(models.FingerprintAssessment)
        .where(models.FingerprintAssessment.asset_id == asset_id)
        .order_by(desc(models.FingerprintAssessment.decision_ts))
        .limit(1)
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"no fingerprint for asset {asset_id}")
    return _fingerprint_row(session, row)


def _prelaunch_row(session: Session, row: models.PrelaunchCandidate) -> PrelaunchRow:
    asset = session.get(models.Asset, row.asset_id)
    chain = session.get(models.Chain, asset.chain_id) if asset else None
    return PrelaunchRow(
        id=row.id,
        asset_id=row.asset_id,
        symbol=asset.symbol if asset else None,
        chain=chain.slug if chain else "unknown",
        decision_ts=row.decision_ts,
        priority_score=row.priority_score,
        drivers=row.drivers,
    )


def _forecast_row(session: Session, row: models.Forecast) -> ForecastRow:
    asset = session.get(models.Asset, row.asset_id)
    chain = session.get(models.Chain, asset.chain_id) if asset else None
    return ForecastRow(
        id=row.id,
        asset_id=row.asset_id,
        symbol=asset.symbol if asset else None,
        chain=chain.slug if chain else "unknown",
        decision_ts=row.decision_ts,
        p_ignition_24h=row.p_ignition_24h,
        p_collapse_24h=row.p_collapse_24h,
        expected_hours_to_peak=row.expected_hours_to_peak,
        expected_hours_to_collapse=row.expected_hours_to_collapse,
        calibration_bucket=row.calibration_bucket,
        calibrated=row.calibrated,
        model_version=row.model_version,
        details=row.details,
    )


@app.get("/radar/prelaunch", response_model=list[PrelaunchRow])
def radar_prelaunch(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PrelaunchRow]:
    latest_ts = session.scalar(select(func.max(models.PrelaunchCandidate.decision_ts)))
    if latest_ts is None:
        return []
    rows = session.scalars(
        select(models.PrelaunchCandidate)
        .where(models.PrelaunchCandidate.decision_ts == latest_ts)
        .order_by(desc(models.PrelaunchCandidate.priority_score))
        .limit(limit)
    ).all()
    return [_prelaunch_row(session, row) for row in rows]


@app.get("/forecasts", response_model=list[ForecastRow])
def forecasts(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ForecastRow]:
    latest_ts = session.scalar(select(func.max(models.Forecast.decision_ts)))
    if latest_ts is None:
        return []
    rows = session.scalars(
        select(models.Forecast)
        .where(models.Forecast.decision_ts == latest_ts)
        .order_by(desc(models.Forecast.p_collapse_24h))
        .limit(limit)
    ).all()
    return [_forecast_row(session, row) for row in rows]


@app.get("/narrative/clusters", response_model=list[NarrativeClusterRow])
def narrative_clusters(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NarrativeClusterRow]:
    rows = session.scalars(
        select(models.NarrativeCluster)
        .order_by(desc(models.NarrativeCluster.last_seen_at))
        .limit(limit)
    ).all()
    return [
        NarrativeClusterRow(
            id=row.id,
            cluster_key=row.cluster_key,
            seed_topic=row.seed_topic,
            mention_count=row.mention_count,
            first_seen_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
        )
        for row in rows
    ]


@app.get("/catalysts", response_model=list[CatalystRow])
def catalysts(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CatalystRow]:
    rows = session.scalars(
        select(models.Catalyst).order_by(desc(models.Catalyst.scheduled_at)).limit(limit)
    ).all()
    output: list[CatalystRow] = []
    for row in rows:
        asset = session.get(models.Asset, row.asset_id)
        output.append(
            CatalystRow(
                id=row.id,
                asset_id=row.asset_id,
                symbol=asset.symbol if asset else None,
                catalyst_type=row.catalyst_type,
                scheduled_at=row.scheduled_at,
                published_at=row.published_at,
                confidence=row.confidence,
            )
        )
    return output


def _lifecycle_event_row(session: Session, row: models.LifecycleEvent) -> LifecycleEventRow:
    asset = session.get(models.Asset, row.asset_id)
    chain = session.get(models.Chain, asset.chain_id) if asset else None
    return LifecycleEventRow(
        id=row.id,
        asset_id=row.asset_id,
        symbol=asset.symbol if asset else None,
        chain=chain.slug if chain else "unknown",
        phase=row.phase,
        event_type=row.event_type,
        ts=row.ts,
        observed_at=row.observed_at,
        confidence=row.confidence,
        details=row.details,
    )


@app.get("/lifecycle/events", response_model=list[LifecycleEventRow])
def lifecycle_events(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    phase: str | None = None,
) -> list[LifecycleEventRow]:
    stmt = select(models.LifecycleEvent).order_by(desc(models.LifecycleEvent.ts)).limit(limit)
    if phase:
        stmt = stmt.where(models.LifecycleEvent.phase == phase)
    rows = session.scalars(stmt).all()
    return [_lifecycle_event_row(session, row) for row in rows]


@app.get("/lifecycle/current", response_model=list[LifecycleEventRow])
def lifecycle_current(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[LifecycleEventRow]:
    sub = (
        select(
            models.LifecycleEvent.asset_id,
            func.max(models.LifecycleEvent.ts).label("max_ts"),
        )
        .group_by(models.LifecycleEvent.asset_id)
        .subquery()
    )
    rows = session.scalars(
        select(models.LifecycleEvent)
        .join(
            sub,
            (models.LifecycleEvent.asset_id == sub.c.asset_id)
            & (models.LifecycleEvent.ts == sub.c.max_ts),
        )
        .order_by(desc(models.LifecycleEvent.ts))
        .limit(limit)
    ).all()
    return [_lifecycle_event_row(session, row) for row in rows]


@app.get("/lifecycle/alerts", response_model=list[LifecycleTransitionAlertRow])
def lifecycle_transition_alerts(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[LifecycleTransitionAlertRow]:
    """Terminal lifecycle alerts with the transition evidence inline."""
    alerts = session.scalars(
        select(models.Alert)
        .where(models.Alert.alert_type == "lifecycle_transition")
        .order_by(desc(models.Alert.created_at))
        .limit(limit)
    ).all()
    output: list[LifecycleTransitionAlertRow] = []
    terminal_phases = {"collapse", "rugged", "dead"}
    for alert in alerts:
        ref = alert.score_snapshot_ref or ""
        prefix = "lifecycle:"
        if not ref.startswith(prefix):
            continue
        try:
            event_id = int(ref.removeprefix(prefix))
        except ValueError:
            continue
        event = session.get(models.LifecycleEvent, event_id)
        if event is None or event.phase not in terminal_phases:
            continue
        asset = session.get(models.Asset, alert.asset_id)
        chain = session.get(models.Chain, asset.chain_id) if asset else None
        output.append(
            LifecycleTransitionAlertRow(
                id=alert.id,
                asset_id=alert.asset_id,
                symbol=asset.symbol if asset else None,
                chain=chain.slug if chain else "unknown",
                phase=event.phase,
                event_id=event.id,
                event_ts=event.ts,
                confidence=event.confidence,
                created_at=alert.created_at,
                state=alert.state,
                message=alert.message,
                evidence=event.details or {},
            )
        )
    return output


@app.get("/archive/manifests", response_model=list[ArchiveManifestRow])
def archive_manifests(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ArchiveManifestRow]:
    rows = session.scalars(
        select(models.ArchiveManifest)
        .order_by(desc(models.ArchiveManifest.created_at))
        .limit(limit)
    ).all()
    sources = {
        source.id: source.name
        for source in session.scalars(
            select(models.Source).where(models.Source.id.in_({row.source_id for row in rows}))
        )
    }
    return [
        ArchiveManifestRow(
            id=row.id,
            object_key=row.object_key,
            source_name=sources.get(row.source_id, "unknown"),
            partition_year=row.partition_year,
            partition_month=row.partition_month,
            row_count=row.row_count,
            byte_size=row.byte_size,
            first_observed_at=row.first_observed_at,
            last_observed_at=row.last_observed_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


@app.get("/retention/runs", response_model=list[RetentionRunRow])
def retention_runs(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> list[RetentionRunRow]:
    """Retention-autopilot history: lake totals + growth per pass. The latest
    pass's growth is also visible in Feed Health as the ``lake`` component."""
    rows = session.scalars(
        select(models.RetentionRun).order_by(desc(models.RetentionRun.ts)).limit(limit)
    ).all()
    return [
        RetentionRunRow(
            id=row.id,
            ts=row.ts,
            partitions=row.partitions,
            archived_rows=row.archived_rows,
            byte_size=row.byte_size,
            compacted=row.compacted,
            pruned=row.pruned,
            growth_bytes=row.growth_bytes,
            growth_pct=row.growth_pct,
            duration_sec=row.duration_sec,
        )
        for row in rows
    ]


@app.get("/retention/growth", response_model=RetentionGrowthRow)
def retention_growth(
    session: DbSession,
    limit: Annotated[int, Query(ge=2, le=200)] = 30,
) -> RetentionGrowthRow:
    """Lake-growth trendline data: retention-pass history plus a projected
    disk-full horizon extrapolated from the recent growth rate (linear fit of
    ``byte_size`` over elapsed time, capped by ``ARCHIVE_LAKE_MAX_BYTES``)."""
    from ops.retention import project_lake_growth

    rows = session.scalars(
        select(models.RetentionRun).order_by(desc(models.RetentionRun.ts)).limit(limit)
    ).all()
    settings = get_settings()
    projection = project_lake_growth(rows, max_bytes=settings.archive_lake_max_bytes)
    return RetentionGrowthRow(
        runs=[
            RetentionRunRow(
                id=row.id,
                ts=row.ts,
                partitions=row.partitions,
                archived_rows=row.archived_rows,
                byte_size=row.byte_size,
                compacted=row.compacted,
                pruned=row.pruned,
                growth_bytes=row.growth_bytes,
                growth_pct=row.growth_pct,
                duration_sec=row.duration_sec,
            )
            for row in rows
        ],
        max_bytes=settings.archive_lake_max_bytes,
        growth_rate_bytes_per_hour=projection["growth_rate_bytes_per_hour"],
        projected_full_at=projection["projected_full_at"],
        days_to_full=projection["days_to_full"],
        pct_full=projection["pct_full"],
    )


@app.get("/rpc/pool", response_model=list[RpcPoolChainRow])
def rpc_pool_states(session: DbSession) -> list[RpcPoolChainRow]:
    """Per-chain RPC pool state, preferring the worker's persisted snapshots.

    The worker and API commonly run in separate processes. The API therefore
    reads the latest endpoint snapshot from the database and only falls back to
    its local in-memory pool before the first worker scan.
    """
    latest = (
        select(
            models.RpcPoolSnapshot.chain_slug,
            models.RpcPoolSnapshot.url,
            func.max(models.RpcPoolSnapshot.ts).label("max_ts"),
        )
        .group_by(models.RpcPoolSnapshot.chain_slug, models.RpcPoolSnapshot.url)
        .subquery()
    )
    persisted = session.scalars(
        select(models.RpcPoolSnapshot).join(
            latest,
            (models.RpcPoolSnapshot.chain_slug == latest.c.chain_slug)
            & (models.RpcPoolSnapshot.url == latest.c.url)
            & (models.RpcPoolSnapshot.ts == latest.c.max_ts),
        )
    ).all()
    persisted_by_chain: dict[str, list[models.RpcPoolSnapshot]] = defaultdict(list)
    for row in persisted:
        persisted_by_chain[row.chain_slug].append(row)

    output: list[RpcPoolChainRow] = []
    for chain_slug in POOL_CHAINS:
        rows = persisted_by_chain.get(chain_slug)
        if rows:
            down_count = sum(row.down for row in rows)
            degraded_count = sum(not row.down and row.health < HEALTH_START for row in rows)
            endpoints = [
                RpcPoolEndpointRow(
                    url=row.url,
                    health=row.health,
                    consecutive_failures=row.consecutive_failures,
                    down=row.down,
                    last_probe_at=row.last_probe_at,
                    last_probe_ok=row.last_probe_ok,
                    probe_count=row.probe_count,
                    probe_successes=row.probe_successes,
                    probe_failures=row.probe_failures,
                    probe_history=[
                        RpcPoolProbeRow(ts=probe["ts"], ok=bool(probe["ok"]))
                        for probe in (row.probe_history or [])
                        if isinstance(probe, dict) and probe.get("ts") is not None
                    ],
                )
                for row in rows
            ]
        else:
            states = get_rpc_pool(chain_slug).snapshot()
            down_count = sum(state.down for state in states)
            degraded_count = sum(not state.down and state.health < HEALTH_START for state in states)
            endpoints = [
                RpcPoolEndpointRow(
                    url=state.url,
                    health=state.health,
                    consecutive_failures=state.consecutive_failures,
                    down=state.down,
                    last_probe_at=state.last_probe_at,
                    last_probe_ok=state.last_probe_ok,
                    probe_count=state.probe_count,
                    probe_successes=state.probe_successes,
                    probe_failures=state.probe_failures,
                    probe_history=[
                        RpcPoolProbeRow(ts=probe.ts, ok=probe.ok) for probe in state.probe_history
                    ],
                )
                for state in states
            ]
        state = "red" if down_count else ("yellow" if degraded_count else "ok")
        output.append(
            RpcPoolChainRow(
                chain=chain_slug,
                state=state,
                down_count=down_count,
                degraded_count=degraded_count,
                endpoints=endpoints,
            )
        )
    return output


VELOCITY_FEATURE_NAMES = ("kol_velocity", "github_star_velocity", "hf_download_velocity")


@app.get("/features/velocity", response_model=list[VelocityFeatureRow])
def features_velocity(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[VelocityFeatureRow]:
    """Latest narrative dev-activity velocity features per asset."""
    latest_ts = session.scalar(select(func.max(models.Feature.decision_ts)))
    if latest_ts is None:
        return []
    rows = session.scalars(
        select(models.Feature).where(
            models.Feature.decision_ts == latest_ts,
            models.Feature.feature_name.in_(VELOCITY_FEATURE_NAMES),
        )
    ).all()
    by_asset: dict[int, dict[str, models.Feature]] = defaultdict(dict)
    for row in rows:
        by_asset[row.asset_id][row.feature_name] = row

    def _value(features: dict[str, models.Feature], name: str) -> tuple[float | None, bool]:
        row = features.get(name)
        if row is None:
            return None, True
        return (None, True) if row.missing_flag else (row.feature_value, False)

    output: list[VelocityFeatureRow] = []
    for asset_id, features in by_asset.items():
        asset = session.get(models.Asset, asset_id)
        chain = session.get(models.Chain, asset.chain_id) if asset else None
        kol, kol_missing = _value(features, "kol_velocity")
        stars, stars_missing = _value(features, "github_star_velocity")
        downloads, downloads_missing = _value(features, "hf_download_velocity")
        output.append(
            VelocityFeatureRow(
                asset_id=asset_id,
                symbol=asset.symbol if asset else "UNKNOWN",
                chain=chain.slug if chain else "unknown",
                decision_ts=latest_ts,
                kol_velocity=kol,
                kol_velocity_missing=kol_missing,
                github_star_velocity=stars,
                github_star_velocity_missing=stars_missing,
                hf_download_velocity=downloads,
                hf_download_velocity_missing=downloads_missing,
            )
        )
    output.sort(
        key=lambda row: (
            row.github_star_velocity or 0.0,
            row.hf_download_velocity or 0.0,
            row.kol_velocity or 0.0,
        ),
        reverse=True,
    )
    return output[:limit]


@app.post("/backtest/run", response_model=TriggerResponse)
def trigger_backtest(payload: BacktestRequest) -> TriggerResponse:
    """Launch a backtest from the UI, including lake replay mode."""
    if payload.feature_source not in ("sql", "lake"):
        raise HTTPException(status_code=422, detail="feature_source must be 'sql' or 'lake'")
    import threading

    def _run() -> None:
        from backtest.runner import run_backtest
        from storage.database import SessionLocal

        with SessionLocal() as session:
            run_backtest(
                session,
                start=payload.start,
                end=payload.end,
                top_k=payload.top_k,
                forward_hours=payload.forward_hours,
                feature_source=payload.feature_source,
            )
            session.commit()

    threading.Thread(target=_run, name="api-backtest", daemon=True).start()
    return TriggerResponse(
        status="accepted", message=f"Backtest started with feature_source={payload.feature_source}"
    )


@app.get("/backtest/results", response_model=list[BacktestResultRow])
def backtest_results(
    session: DbSession, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[BacktestResultRow]:
    runs = session.scalars(
        select(models.BacktestRun).order_by(desc(models.BacktestRun.started_at)).limit(limit)
    ).all()
    out: list[BacktestResultRow] = []
    for run in runs:
        metrics: dict[str, float] = defaultdict(float)
        for result in session.scalars(
            select(models.BacktestResult).where(models.BacktestResult.run_id == run.id)
        ):
            key = (
                result.metric_name
                if not result.chain_slug
                else f"{result.chain_slug}.{result.metric_name}"
            )
            metrics[key] = result.metric_value
        out.append(
            BacktestResultRow(
                run_id=run.id,
                status=run.status,
                started_at=run.started_at,
                cutoff_start=run.cutoff_start,
                cutoff_end=run.cutoff_end,
                model_version=run.model_version,
                metrics=dict(metrics),
            )
        )
    return out


@app.get("/ops/console", response_model=OpsConsoleResponse)
def ops_console(session: DbSession) -> OpsConsoleResponse:
    """Live ops console: last scan pipeline stage counts, notifier health, and recent alerts."""
    scan = latest_scan_result(session)
    scan_row = None
    if scan:
        scan_row = ScanResultRow(
            id=scan.id,
            ts=scan.ts,
            duration_sec=scan.duration_sec,
            pairs=scan.pairs,
            profiles=scan.profiles,
            scores=scan.scores,
            ignition_events=scan.ignition_events,
            fingerprints=scan.fingerprints,
            forecasts=scan.forecasts,
            lifecycle=scan.lifecycle,
            narrative=scan.narrative,
            mempool=scan.mempool,
            lp_removals=scan.lp_removals,
            prelaunch=scan.prelaunch,
            catalysts=scan.catalysts,
            archive=scan.archive,
            ntfy_sent=scan.ntfy_sent,
            rpc_pool_notifications=scan.rpc_pool_notifications,
            rpc_pool_snapshots=scan.rpc_pool_snapshots,
            state=scan.state,
            error_message=scan.error_message,
            details=scan.details,
            pre_scan_health=(scan.details or {}).get("pre_scan_health", {}),
        )
    notifier_health_row = None
    lake_budget_row = None
    notifier_components = latest_health(session, limit=50)
    from ops.retention import project_lake_growth

    retention_rows = session.scalars(
        select(models.RetentionRun).order_by(desc(models.RetentionRun.ts)).limit(30)
    ).all()
    projection = project_lake_growth(
        retention_rows, max_bytes=get_settings().archive_lake_max_bytes
    )
    for component in notifier_components:
        if component.component == "notifier":
            notifier_health_row = NotifierHealthRow(
                component=component.component,
                state=component.state,
                ts=component.ts,
                message=component.message,
                error_count=component.error_count,
            )
        elif component.component == "lake_budget":
            lake_budget_row = LakeBudgetHealthRow(
                component=component.component,
                state=component.state,
                ts=component.ts,
                message=component.message,
                error_count=component.error_count,
                projected_full_at=projection["projected_full_at"],
                days_to_full=projection["days_to_full"],
            )
        if notifier_health_row is not None and lake_budget_row is not None:
            break
    alert_rows = session.scalars(
        select(models.Alert)
        .where(models.Alert.notified_at.isnot(None))
        .order_by(desc(models.Alert.notified_at))
        .limit(20)
    ).all()
    recent_alerts = [_alert_row(session, row) for row in alert_rows]
    return OpsConsoleResponse(
        last_scan=scan_row,
        notifier_health=notifier_health_row,
        lake_budget=lake_budget_row,
        recent_alerts=recent_alerts,
    )


# ── Engine Control Endpoints ────────────────────────────────────────────────


@app.get("/engine/status", response_model=EngineStatusResponse)
def engine_status() -> EngineStatusResponse:
    """Live engine runtime status: uptime, scan phase, progress counters."""
    snap = engine_state.snapshot()
    scan_snap = snap["scan"]
    return EngineStatusResponse(
        status=snap["status"],
        uptime_sec=snap["uptime_sec"],
        total_iterations=snap["total_iterations"],
        scan_interval_seconds=snap["scan_interval_seconds"],
        scan=EngineScanProgressRow(
            phase=scan_snap["phase"],
            phase_message=scan_snap["phase_message"],
            duration_sec=scan_snap["duration_sec"],
            iteration=scan_snap["iteration"],
            pairs=scan_snap["pairs"],
            scores=scan_snap["scores"],
            forecasts=scan_snap["forecasts"],
            lifecycle=scan_snap["lifecycle"],
            narrative=scan_snap["narrative"],
            catalysts=scan_snap["catalysts"],
            ignition_events=scan_snap["ignition_events"],
            fingerprints=scan_snap["fingerprints"],
            archive=scan_snap["archive"],
            ntfy_sent=scan_snap["ntfy_sent"],
            rpc_pool_snapshots=scan_snap["rpc_pool_snapshots"],
            error_message=scan_snap["error_message"],
        ),
    )


@app.post("/engine/seed", response_model=SeedResponse)
def engine_seed() -> SeedResponse:
    """Seed fixture data into the database for first-run experience."""
    from storage.seed import seed_reference_data

    seed_reference_data()
    return SeedResponse(
        status="ok",
        message="Seeded reference data (chains, sources, venues)",
    )


@app.post("/engine/scan", response_model=TriggerResponse)
def engine_trigger_scan() -> TriggerResponse:
    """Trigger a manual ingestion scan. Runs in a background thread."""
    import threading

    from engine.state import ScanPhase

    current = engine_state.scan.phase
    if current not in (ScanPhase.IDLE, ScanPhase.COMPLETED, ScanPhase.ERROR):
        return TriggerResponse(
            status="rejected",
            message=f"Scan already in progress (phase={current.value})",
        )

    def _run() -> None:
        try:
            engine_state.mark_scanning(iteration=None, message="Manual scan triggered from API")
            from ingestion.worker import run_once

            result = run_once()
            engine_state.mark_scan_result(result)
            engine_state.mark_completed()
            log.info("api_manual_scan_complete", result=result)
        except Exception as exc:  # noqa: BLE001
            engine_state.mark_error(str(exc))
            log.exception("api_manual_scan_failed", error=str(exc))

    threading.Thread(target=_run, name="api-manual-scan", daemon=True).start()
    return TriggerResponse(
        status="accepted",
        message="Manual scan started",
    )


@app.post("/engine/forecast", response_model=TriggerResponse)
def engine_trigger_forecast() -> TriggerResponse:
    """Trigger forecast model training."""
    import threading

    from engine.state import ScanPhase

    current = engine_state.scan.phase
    if current not in (ScanPhase.IDLE, ScanPhase.COMPLETED, ScanPhase.ERROR):
        return TriggerResponse(
            status="rejected",
            message=f"Engine busy (phase={current.value})",
        )

    def _run() -> None:
        try:
            engine_state.mark_forecasting()
            from forecast.engine import maybe_run_forecast

            result = maybe_run_forecast()
            engine_state.mark_completed()
            log.info("api_manual_forecast_complete", result=result)
        except Exception as exc:  # noqa: BLE001
            engine_state.mark_error(str(exc))
            log.exception("api_manual_forecast_failed", error=str(exc))

    threading.Thread(target=_run, name="api-manual-forecast", daemon=True).start()
    return TriggerResponse(status="accepted", message="Forecast training started")


@app.post("/engine/retention", response_model=TriggerResponse)
def engine_trigger_retention() -> TriggerResponse:
    """Trigger retention autopilot pass."""
    import threading

    from engine.state import ScanPhase

    current = engine_state.scan.phase
    if current not in (ScanPhase.IDLE, ScanPhase.COMPLETED, ScanPhase.ERROR):
        return TriggerResponse(
            status="rejected",
            message=f"Engine busy (phase={current.value})",
        )

    def _run() -> None:
        try:
            engine_state.mark_retention()
            from ops.retention import maybe_run_retention

            result = maybe_run_retention()
            engine_state.mark_completed()
            log.info("api_manual_retention_complete", result=result)
        except Exception as exc:  # noqa: BLE001
            engine_state.mark_error(str(exc))
            log.exception("api_manual_retention_failed", error=str(exc))

    threading.Thread(target=_run, name="api-manual-retention", daemon=True).start()
    return TriggerResponse(status="accepted", message="Retention pass started")


# ── WebSocket Stream ────────────────────────────────────────────────────────


@app.websocket("/ws/prices")
async def ws_price_stream(websocket: WebSocket) -> None:
    """WebSocket endpoint for live price updates.

    Clients receive JSON messages with price snapshots as they arrive.
    Format: {"type":"price_update","asset_id":1,"symbol":"FOO",...}
    """
    await websocket.accept()
    queue = price_stream_broker.connect()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_json(event)
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        price_stream_broker.disconnect(queue)


@app.get("/ws/price/count")
def ws_price_client_count() -> dict:
    """Return the number of connected WebSocket price clients."""
    return {"connected_clients": price_stream_broker.connected_count}


# -- Activity Stream WebSocket -----------------------------------------------


@app.websocket("/ws/nightcrawlers/activity")
async def ws_activity_stream(websocket: WebSocket) -> None:
    """WebSocket endpoint for live crawler activity updates.

    Clients receive JSON messages with activity events as they arrive.
    Format: {"type":"activity","source":"coingecko",...}
    """
    await websocket.accept()
    queue = activity_stream_broker.connect()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_json(event)
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        activity_stream_broker.disconnect(queue)


@app.get("/ws/activity/count")
def ws_activity_client_count() -> dict:
    """Return the number of connected WebSocket activity clients."""
    return {"connected_clients": activity_stream_broker.connected_count}


# ── SSE Stream ──────────────────────────────────────────────────────────────


@app.get("/engine/stream")
def engine_sse_stream():
    """Server-Sent Events stream for real-time engine phase updates.

    Clients receive a full state snapshot on connect, then incremental
    ``event: <phase>`` messages whenever the engine transitions.

    Event format::

        event: scanning
        data: {"type":"scanning","status":"running",...}

        event: completed
        data: {"type":"completed","status":"running",...}
    """

    async def event_generator():
        # Send initial state snapshot immediately
        initial = engine_state.snapshot()
        yield f"event: init\ndata: {json.dumps(initial)}\n\n"

        # Subscribe to future updates
        queue = sse_broker.connect()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    event_type = event.get("type", "update")
                    event_data = {k: v for k, v in event.items() if k != "type"}
                    yield f"event: {event_type}\ndata: {json.dumps(event_data)}\n\n"
                except TimeoutError:
                    # Send a keepalive comment every 30s to prevent proxy timeouts
                    yield f": keepalive {time.time():.0f}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sse_broker.disconnect(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Data Lake Endpoints ─────────────────────────────────────────────────────


@app.get("/data/labels/progress")
def data_labels_progress(session: DbSession) -> dict:
    """Label generation progress toward the training threshold."""
    from data_lake.labels import label_generation_progress

    return label_generation_progress(session)


@app.post("/data/signal/score")
def data_signal_score(session: DbSession) -> dict:
    """Score a batch of recent data points for signal strength."""
    from data_lake.signal import score_batch

    result = score_batch(session)
    return {
        "total_scored": result.total_scored,
        "actionable_count": result.actionable_count,
        "noise_count": result.noise_count,
        "avg_signal": result.avg_signal,
        "top_signals": [
            {
                "source_table": s.source_table,
                "record_id": s.record_id,
                "signal_score": s.signal_score,
                "novelty_score": s.novelty_score,
                "corroboration_score": s.corroboration_score,
                "temporal_score": s.temporal_score,
                "magnitude_score": s.magnitude_score,
                "reasons": s.reasons[:3],
                "actionable": s.actionable,
            }
            for s in result.top_signals[:20]
        ],
    }


@app.post("/data/densify-labels")
def data_densify_labels(session: DbSession) -> dict:
    """Densify forecast labels from existing market snapshots."""
    from data_lake.labels import generate_dense_labels

    counts = generate_dense_labels(session)
    return counts


@app.get("/data/confidence", response_model=dict)
def data_confidence(session: DbSession) -> dict:
    """Confidence dashboard data: label progress, scoring breakdown, scan history."""
    from data_lake.manager import get_confidence_dashboard_data

    return get_confidence_dashboard_data(session)


@app.get("/webhooks", response_model=list[dict])
def webhook_list(session: DbSession) -> list[dict]:
    """List all registered webhooks."""
    from data_lake.webhooks import list_webhooks

    webhooks = list_webhooks(session)
    return [
        {
            "id": w.id,
            "url": w.url,
            "name": w.name,
            "event_types": w.event_types,
            "enabled": w.enabled,
            "cooldown_seconds": w.cooldown_seconds,
            "chain_filter": w.chain_filter,
            "min_signal_score": w.min_signal_score,
            "last_dispatched_at": w.last_dispatched_at,
            "created_at": w.created_at,
        }
        for w in webhooks
    ]


@app.post("/webhooks/register", response_model=dict)
def webhook_register(
    session: DbSession,
    webhook_url: str = "http://localhost:8080/webhook",
    webhook_name: str = "custom",
    webhook_events: str = "ignition_detected,lifecycle_transition,high_signal_scan",
    webhook_secret: str | None = None,
) -> dict:
    """Register a new webhook. Parameters passed via query strings for GUI compatibility."""
    from data_lake.webhooks import register_webhook

    event_types = [e.strip() for e in webhook_events.split(",") if e.strip()]
    webhook = register_webhook(
        session,
        url=webhook_url,
        name=webhook_name,
        event_types=event_types,
        secret=webhook_secret,
    )
    return {
        "id": webhook.id,
        "url": webhook.url,
        "name": webhook.name,
        "event_types": webhook.event_types,
        "status": "registered",
    }


@app.post("/webhooks/{webhook_id}/delete", response_model=dict)
def webhook_delete(webhook_id: int, session: DbSession) -> dict:
    """Delete a webhook by ID. Uses POST for GUI compatibility (Streamlit api_post helper)."""
    from data_lake.webhooks import delete_webhook

    deleted = delete_webhook(session, webhook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"webhook {webhook_id} not found")
    return {"status": "deleted", "webhook_id": webhook_id}


@app.get("/webhooks/dispatches")
def webhook_dispatches(
    session: DbSession,
    limit: int = 50,
    webhook_id: int | None = None,
) -> list[dict]:
    """Get recent webhook dispatch history."""
    from data_lake.webhooks import webhook_dispatch_history

    dispatches = webhook_dispatch_history(session, webhook_id=webhook_id, limit=limit)
    return [
        {
            "id": d.id,
            "webhook_config_id": d.webhook_config_id,
            "event_type": d.event_type,
            "dispatched_at": d.dispatched_at,
            "success": d.success,
            "status_code": d.status_code,
            "error_message": d.error_message,
            "duration_ms": d.duration_ms,
        }
        for d in dispatches
    ]


@app.get("/nightcrawlers/leaderboard")
def nightcrawler_leaderboard(
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> dict:
    """Crawler performance leaderboard: SNR per source with weekly trend.

    Returns each source ranked by signal-to-noise ratio over the lookback
    window, plus a ``sparkline`` array of weekly SNR values for the GUI
    to render as an inline trend chart.
    """
    cutoff = utc_now() - timedelta(days=days)

    # Fetch all nightcrawler raw evidence in the window
    rows = session.scalars(
        select(models.RawEvidenceItem)
        .join(models.Source, models.Source.id == models.RawEvidenceItem.source_id)
        .where(
            models.Source.name.like("nightcrawler:%"),
            models.RawEvidenceItem.observed_at >= cutoff,
        )
        .order_by(models.RawEvidenceItem.observed_at)
    ).all()

    # Bucket items by source and ISO-week
    source_buckets: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"total": 0, "actionable": 0})
    )
    source_totals: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "actionable": 0})

    for row in rows:
        source = session.get(models.Source, row.source_id)
        name = source.name.removeprefix("nightcrawler:") if source else "unknown"
        payload = row.payload or {}
        items = payload.get("items", [])
        count = payload.get("count", len(items))

        # Determine if this batch is actionable (has signal)
        signal_score, total_engagement, token_mentions = compute_activity_signal_score(items)
        has_signal = signal_score >= 10 or bool(token_mentions)

        # ISO-week bucket key
        observed = row.observed_at or utc_now()
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        week_key = observed.astimezone(UTC).strftime("%Y-W%U")

        bucket = source_buckets[name][week_key]
        bucket["total"] += count
        if has_signal:
            bucket["actionable"] += count

        source_totals[name]["total"] += count
        if has_signal:
            source_totals[name]["actionable"] += count

    # Also pull crawler health stats from the orchestrator
    from crawlers.orchestrator import get_nightcrawler_orchestrator

    status = get_nightcrawler_orchestrator().get_status()

    # Build leaderboard entries
    entries: list[dict] = []
    for name, totals in source_totals.items():
        total = totals["total"]
        actionable = totals["actionable"]
        snr = (actionable / total) if total > 0 else 0.0

        # Build sparkline: weekly SNR values in chronological order
        weeks_sorted = sorted(source_buckets[name].keys())
        sparkline = []
        for wk in weeks_sorted:
            wb = source_buckets[name][wk]
            w_total = wb["total"]
            w_actionable = wb["actionable"]
            sparkline.append(round((w_actionable / w_total) * 100, 1) if w_total > 0 else 0.0)

        health = status.get(name, {})
        entries.append(
            {
                "source": name,
                "total_items": total,
                "actionable_items": actionable,
                "snr": round(snr, 3),
                "snr_pct": round(snr * 100, 1),
                "reliability": health.get("reliability", 0.0),
                "error_rate": health.get("error_rate", 0.0),
                "total_runs": health.get("total_runs", 0),
                "frequency_multiplier": health.get("frequency_multiplier", 1.0),
                "sparkline": sparkline,
                "weeks_with_data": len(sparkline),
            }
        )

    # Sort by SNR descending
    entries.sort(key=lambda e: e["snr"], reverse=True)

    # Assign rank
    for i, entry in enumerate(entries):
        entry["rank"] = i + 1

    return {
        "lookback_days": days,
        "total_sources": len(entries),
        "entries": entries,
    }


@app.get("/nightcrawlers/status")
def nightcrawler_status() -> dict:
    """Status of all Night Crawler crawlers."""
    from crawlers.orchestrator import get_nightcrawler_orchestrator

    return get_nightcrawler_orchestrator().get_status()


@app.get("/nightcrawlers/heuristics")
def nightcrawler_heuristics() -> dict:
    """Heuristics engine state: source reliability and learned patterns."""
    from crawlers.orchestrator import get_nightcrawler_orchestrator

    return get_nightcrawler_orchestrator().heuristics.summarize()


@app.get("/nightcrawlers/activity")
def nightcrawler_activity(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    """Recent crawler activity items from raw evidence, newest first.

    Each item includes source name, item count, observed timestamp,
    and a signal score derived from token mentions and engagement.
    """
    rows = session.scalars(
        select(models.RawEvidenceItem)
        .join(models.Source, models.Source.id == models.RawEvidenceItem.source_id)
        .where(models.Source.name.like("nightcrawler:%"))
        .order_by(desc(models.RawEvidenceItem.observed_at))
        .limit(limit)
    ).all()

    activities: list[dict] = []
    for row in rows:
        try:
            source = session.get(models.Source, row.source_id)
            source_name = source.name.removeprefix("nightcrawler:") if source else "unknown"
            payload = row.payload or {}
            items = payload.get("items", [])
            count = payload.get("count", len(items))

            signal_score, total_engagement, token_mentions = compute_activity_signal_score(items)
            platform = ""
            for item in items[:5]:
                p = item.get("metrics", {}).get("platform", "")
                if p:
                    platform = p
                    break

            activities.append(
                {
                    "source": source_name,
                    "platform": platform,
                    "item_count": count,
                    "observed_at": str(row.observed_at)[:19] if row.observed_at else "",
                    "signal_score": signal_score,
                    "token_mentions": token_mentions[:5],
                    "total_engagement": total_engagement,
                }
            )
        except Exception:  # noqa: BLE001
            continue

    return activities


# ── LLM Endpoints ──────────────────────────────────────────────────────────


@app.get("/llm/health")
def llm_health() -> dict:
    """Check local LLM (Ollama) health and model availability."""
    from llm.engine import llm_engine

    health = llm_engine.check_health()
    return {
        "connected": health.connected,
        "model": health.model,
        "available": health.available,
        "last_check": health.last_check,
        "error": health.error,
        "enabled": get_settings().llm_enabled,
    }


# In-process TTL cache for on-demand LLM predictions, keyed by asset_id.
# A fresh Ollama call costs ~2s per token, and the GUI reruns frequently, so
# repeated requests within the TTL window are served from memory instead of
# re-generating. Never persists across restarts; see ``llm_predict_cache_ttl_seconds``.
_LLM_PREDICT_CACHE: dict[int, tuple[float, dict]] = {}


@app.post("/llm/predict/{asset_id}")
def llm_predict_token(asset_id: int, session: DbSession) -> dict:
    """Get an LLM-enhanced prediction for a single token.

    Results are cached in-process for ``llm_predict_cache_ttl_seconds``;
    the response includes a ``cached`` flag so callers can tell whether the
    text was served from memory or freshly generated.
    """
    now = time.time()
    hit = _LLM_PREDICT_CACHE.get(asset_id)
    if hit is not None and (now - hit[0]) < get_settings().llm_predict_cache_ttl_seconds:
        payload = dict(hit[1])
        payload["cached"] = True
        return payload

    from llm.engine import llm_engine

    asset = session.get(models.Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    # Get latest features
    latest_ts = session.scalar(
        select(func.max(models.Feature.decision_ts)).where(models.Feature.asset_id == asset_id)
    )
    if not latest_ts:
        raise HTTPException(status_code=404, detail=f"no features for asset {asset_id}")
    features = {
        row.feature_name: row.feature_value
        for row in session.scalars(
            select(models.Feature).where(
                models.Feature.asset_id == asset_id,
                models.Feature.decision_ts == latest_ts,
            )
        )
    }
    # Get latest score for rule-based values
    score = session.scalar(
        select(models.Score)
        .where(models.Score.asset_id == asset_id)
        .order_by(desc(models.Score.decision_ts))
        .limit(1)
    )
    pred = llm_engine.predict(
        asset_id=asset_id,
        symbol=asset.symbol,
        features=features,
        rule_hype=score.hype if score else 50.0,
        rule_risk=score.risk if score else 25.0,
        rule_confidence=score.confidence if score else 50.0,
    )
    payload = {
        "asset_id": pred.asset_id,
        "symbol": pred.symbol,
        "narrative_summary": pred.narrative_summary,
        "risk_assessment": pred.risk_assessment,
        "confidence_delta": pred.confidence_delta,
        "hype_delta": pred.hype_delta,
        "risk_delta": pred.risk_delta,
        "key_factors": pred.key_factors,
        "llm_model": pred.llm_model,
        "latency_ms": pred.latency_ms,
    }
    # Only cache successful predictions: a failed call returns empty text
    # with neutral deltas and must stay retryable.
    if pred.narrative_summary or pred.risk_assessment:
        payload["cached"] = False
        _LLM_PREDICT_CACHE[asset_id] = (now, payload)
    else:
        payload["cached"] = False
    return payload


@app.get("/llm/narrative/{asset_id}")
def llm_narrative_analysis(asset_id: int, session: DbSession) -> dict:
    """Get a narrative analysis from the local LLM for a token."""
    from llm.engine import llm_engine

    asset = session.get(models.Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    latest_ts = session.scalar(
        select(func.max(models.Feature.decision_ts)).where(models.Feature.asset_id == asset_id)
    )
    features = {}
    if latest_ts:
        features = {
            row.feature_name: row.feature_value
            for row in session.scalars(
                select(models.Feature).where(
                    models.Feature.asset_id == asset_id,
                    models.Feature.decision_ts == latest_ts,
                )
            )
        }
    narrative = llm_engine.narrative_and_risk(asset.symbol, features)
    return {
        "asset_id": asset_id,
        "symbol": asset.symbol,
        "narrative_analysis": narrative[0],
        "risk_assessment": narrative[1],
    }


@app.post("/engine/nightcrawlers", response_model=TriggerResponse)
def engine_trigger_nightcrawlers() -> TriggerResponse:
    """Trigger a Night Crawler pass: crawl all sources, score signals, feed data lake."""
    import threading

    from engine.state import ScanPhase

    current = engine_state.scan.phase
    if current not in (ScanPhase.IDLE, ScanPhase.COMPLETED, ScanPhase.ERROR):
        return TriggerResponse(
            status="rejected",
            message=f"Engine busy (phase={current.value})",
        )

    def _run() -> None:
        try:
            engine_state.mark_scanning(message="Night Crawler pipeline")
            from crawlers.pipeline import run_nightcrawler_pipeline
            from storage.database import SessionLocal

            with SessionLocal() as session:
                result = run_nightcrawler_pipeline(session, force=True)
                session.commit()
            engine_state.mark_completed()
            log.info("api_nightcrawler_complete", result=result)
        except Exception as exc:  # noqa: BLE001
            engine_state.mark_error(str(exc))
            log.exception("api_nightcrawler_failed", error=str(exc))

    threading.Thread(target=_run, name="api-nightcrawler", daemon=True).start()
    return TriggerResponse(status="accepted", message="Night Crawler pipeline started")


@app.get("/llm/calibration")
def llm_calibration_status(session: DbSession) -> dict:
    """Current LLM adaptive weight calibration state."""
    from scoring.llm_calibration import llm_calibrator

    snap = llm_calibrator.get_snapshot(session)
    return {
        "current_weight": snap.current_weight,
        "previous_weight": snap.previous_weight,
        "total_predictions": snap.total_predictions,
        "total_improved": snap.total_improved,
        "total_degraded": snap.total_degraded,
        "improvement_rate": snap.improvement_rate,
        "last_calibration_ts": str(snap.last_calibration_ts) if snap.last_calibration_ts else None,
        "weight_history": snap.weight_history[-20:],
        "enabled": get_settings().llm_calibration_enabled,
        "min_samples": get_settings().llm_calibration_min_samples,
        "window_hours": get_settings().llm_calibration_window_hours,
    }


@app.post("/llm/calibration/run")
def llm_calibration_run(session: DbSession) -> dict:
    """Manually trigger an LLM calibration pass."""
    from scoring.llm_calibration import llm_calibrator

    evaluated = llm_calibrator.evaluate_predictions(session)
    new_weight = llm_calibrator.calibrate(session)
    session.commit()
    return {
        "evaluated": evaluated,
        "new_weight": new_weight,
        "status": "ok",
    }


@app.post("/engine/data-lake", response_model=TriggerResponse)
def engine_trigger_data_lake() -> TriggerResponse:
    """Trigger a data lake pass: signal scoring + label densification + webhooks."""
    import threading

    from engine.state import ScanPhase

    current = engine_state.scan.phase
    if current not in (ScanPhase.IDLE, ScanPhase.COMPLETED, ScanPhase.ERROR):
        return TriggerResponse(
            status="rejected",
            message=f"Engine busy (phase={current.value})",
        )

    def _run() -> None:
        try:
            engine_state.mark_scanning(message="Data lake pass")
            from data_lake.manager import run_data_lake_pass
            from storage.database import SessionLocal

            with SessionLocal() as session:
                result = run_data_lake_pass(session)
                session.commit()
            engine_state.mark_completed()
            log.info("api_data_lake_complete", result=result)
        except Exception as exc:  # noqa: BLE001
            engine_state.mark_error(str(exc))
            log.exception("api_data_lake_failed", error=str(exc))

    threading.Thread(target=_run, name="api-data-lake", daemon=True).start()
    return TriggerResponse(status="accepted", message="Data lake pass started")
