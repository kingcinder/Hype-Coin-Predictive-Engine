"""Data Lake Manager — orchestrates signal scoring, label densification, and webhook dispatch.

Called after each ingestion scan to:
1. Score incoming data for signal strength
2. Densify forecast labels to accelerate training
3. Dispatch webhook alerts for high-signal events
4. Track data lake statistics for the confidence dashboard
"""
from __future__ import annotations

from datetime import datetime, timedelta
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging import get_logger
from common.time import utc_now
from data_lake.labels import generate_dense_labels, label_generation_progress
from data_lake.signal import score_batch
from storage import models
from storage.repository import record_health

log = get_logger(__name__)


def run_data_lake_pass(
    session: Session,
    *,
    decision_ts: datetime | None = None,
) -> dict[str, Any]:
    """Run one full data lake pass: signal scoring + label densification + webhooks.

    Returns a summary dict with counts and progress metrics.
    """
    decision_ts = utc_now() if decision_ts is None else decision_ts
    started = monotonic()
    result: dict[str, Any] = {
        "signal": {},
        "labels": {},
        "webhooks": {},
        "progress": {},
    }

    try:
        # 1. Signal scoring
        signal_result = score_batch(session, decision_ts=decision_ts)
        result["signal"] = {
            "total_scored": signal_result.total_scored,
            "actionable_count": signal_result.actionable_count,
            "noise_count": signal_result.noise_count,
            "avg_signal": signal_result.avg_signal,
        }

        # 2. Label densification
        label_counts = generate_dense_labels(session, decision_ts=decision_ts)
        result["labels"] = label_counts

        # 3. Label generation progress
        progress = label_generation_progress(session)
        result["progress"] = progress

        # 4. Dispatch webhooks for high-signal alerts
        webhook_results = _dispatch_high_signal_webhooks(
            session, signal_result, decision_ts
        )
        result["webhooks"] = {
            "dispatched": len(webhook_results),
            "successful": sum(1 for r in webhook_results if r.success),
            "failed": sum(1 for r in webhook_results if not r.success),
        }

        # 5. Record data lake health
        duration = monotonic() - started
        record_health(
            session,
            component="data_lake",
            state="ok",
            message=(
                f"signal={signal_result.total_scored} scored, "
                f"{signal_result.actionable_count} actionable; "
                f"labels={label_counts['total_labels']} total "
                f"({label_counts['progress_pct']:.0f}% of "
                f"{label_counts['min_samples_required']} required); "
                f"webhooks={result['webhooks']['dispatched']} dispatched; "
                f"duration={duration:.1f}s"
            ),
        )

        log.info(
            "data_lake_pass_complete",
            signal_scored=signal_result.total_scored,
            actionable=signal_result.actionable_count,
            total_labels=label_counts["total_labels"],
            label_progress=label_counts["progress_pct"],
            webhooks_dispatched=result["webhooks"]["dispatched"],
            duration_sec=round(duration, 2),
        )

    except Exception as exc:
        duration = monotonic() - started
        record_health(
            session,
            component="data_lake",
            state="red",
            message=str(exc),
            error_count=1,
        )
        log.exception("data_lake_pass_failed", error=str(exc))
        result["error"] = str(exc)

    return result


def _dispatch_high_signal_webhooks(
    session: Session,
    signal_result: Any,
    decision_ts: datetime,
) -> list[Any]:
    """Dispatch webhooks for high-signal events detected in the batch."""
    from data_lake.webhooks import should_dispatch, build_payload, dispatch_webhook, list_webhooks

    webhooks = list_webhooks(session)
    results: list[Any] = []

    if not webhooks:
        return results

    # Dispatch for high-signal actionable items
    for signal in signal_result.top_signals[:5]:
        if not signal.actionable:
            continue

        for webhook in webhooks:
            if not should_dispatch(session, webhook, "high_signal_scan"):
                continue

            # Build payload with signal details
            extra = {
                "signal_score": signal.signal_score,
                "source_table": signal.source_table,
                "record_id": signal.record_id,
                "reasons": signal.reasons[:3],
            }

            # Try to get asset info for the signal
            asset = None
            chain = None
            if signal.source_table == "market_snapshots":
                snap = session.get(models.MarketSnapshot, signal.record_id)
                if snap:
                    pair = session.get(models.Pair, snap.pair_id)
                    if pair:
                        asset = session.get(models.Asset, pair.base_asset_id)
                        if asset:
                            chain = session.get(models.Chain, asset.chain_id)

            payload = build_payload("high_signal_scan", None, asset, chain, extra)
            result = dispatch_webhook(session, webhook, "high_signal_scan", payload)
            results.append(result)

    # Also dispatch for lifecycle transition alerts
    recent_alerts = session.scalars(
        select(models.Alert).where(
            models.Alert.alert_type == "lifecycle_transition",
            models.Alert.created_at >= decision_ts - timedelta(hours=1),
        )
    ).all()

    for alert in recent_alerts:
        asset = session.get(models.Asset, alert.asset_id)
        chain = session.get(models.Chain, asset.chain_id) if asset else None

        for webhook in webhooks:
            if not should_dispatch(session, webhook, "lifecycle_transition"):
                continue

            payload = build_payload("lifecycle_transition", alert, asset, chain)
            result = dispatch_webhook(session, webhook, "lifecycle_transition", payload)
            results.append(result)

    return results


def get_confidence_dashboard_data(
    session: Session,
) -> dict[str, Any]:
    """Get data for the confidence dashboard view.

    Returns label progress, scoring breakdown, and feature importance data.
    """
    progress = label_generation_progress(session)

    # Get top-scored tokens with feature breakdown
    from storage.repository import latest_scores
    top_scores = latest_scores(session, limit=10, include_black=False, order_by="hype")

    scoring_breakdown = []
    for score in top_scores:
        asset = session.get(models.Asset, score.asset_id)
        chain = session.get(models.Chain, asset.chain_id) if asset else None

        # Get feature importance for this score
        features = session.scalars(
            select(models.Feature).where(
                models.Feature.asset_id == score.asset_id,
                models.Feature.decision_ts == score.decision_ts,
                models.Feature.missing_flag.is_(False),
            )
        ).all()

        feature_importance = {
            f.feature_name: round(f.feature_value, 4)
            for f in features
            if f.feature_name in (
                "five_min_return", "one_hour_return", "volume_acceleration",
                "liquidity_depth", "mention_velocity", "kol_velocity",
                "github_star_velocity", "hf_download_velocity",
            )
        }

        scoring_breakdown.append({
            "asset_id": score.asset_id,
            "symbol": asset.symbol if asset else "UNKNOWN",
            "chain": chain.slug if chain else "unknown",
            "hype": round(score.hype, 2),
            "ethos": round(score.ethos, 2),
            "risk": round(score.risk, 2),
            "liquidity_access": round(score.liquidity_access, 2),
            "confidence": round(score.confidence, 2),
            "research_priority": round(score.research_priority, 2),
            "risk_band": score.risk_band,
            "feature_importance": feature_importance,
        })

    # Get scan history for the chart
    scan_history = session.scalars(
        select(models.ScanResult)
        .order_by(models.ScanResult.ts.desc())
        .limit(20)
    ).all()

    scan_chart = [
        {
            "ts": scan.ts.isoformat() if scan.ts else None,
            "duration_sec": scan.duration_sec,
            "pairs": scan.pairs,
            "scores": scan.scores,
            "forecasts": scan.forecasts,
            "lifecycle": scan.lifecycle,
            "narrative": scan.narrative,
        }
        for scan in reversed(list(scan_history))
    ]

    return {
        "label_progress": progress,
        "scoring_breakdown": scoring_breakdown,
        "scan_history": scan_chart,
    }
