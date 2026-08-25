from __future__ import annotations

from dataclasses import replace as _replace
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import AlertState, AlertType, RiskBand
from common.logging import get_logger
from common.time import utc_now
from engine.price_stream import PriceUpdate, broadcast_price
from features.factory import build_and_persist_features
from ingestion.rpc_pool import get_rpc_pool
from risk_engine.rules import mask_unreliable_forecast
from scoring.ensemble import ensemble_engine
from scoring.formulas import clamp, compute_scores
from storage import models

log = get_logger(__name__)


class ScoringEngine:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _rpc_pool_uncertainty(self, session: Session, asset_id: int) -> float:
        """Translate the asset's chain RPC availability into uncertainty points.

        A healthy endpoint contributes its health score; a down endpoint
        contributes zero. This keeps a partially degraded pool as a graduated
        penalty and makes an all-down pool maximally uncertain. The adjustment
        is disabled when RPC pooling is explicitly disabled because the
        configured single endpoint is then the operator's intentional source.
        """
        if not self.settings.rpc_pool_enabled:
            return 0.0
        asset = session.get(models.Asset, asset_id)
        chain = session.get(models.Chain, asset.chain_id) if asset else None
        if chain is None:
            return 0.0
        states = get_rpc_pool(chain.slug).snapshot()
        if not states:
            return 100.0
        effective_health = sum(
            0.0 if state.down else max(0.0, min(1.0, state.health)) for state in states
        ) / len(states)
        return round(max(0.0, min(100.0, (1.0 - effective_health) * 100.0)), 4)

    def score_assets(
        self,
        session: Session,
        *,
        decision_ts: datetime | None = None,
        asset_ids: list[int] | None = None,
        feature_source: str = "sql",
    ) -> list[models.Score]:
        decision_ts = decision_ts or utc_now()
        feature_map = build_and_persist_features(
            session,
            decision_ts=decision_ts,
            asset_ids=asset_ids,
            feature_source=feature_source,
        )
        scores: list[models.Score] = []
        price_updates: list[PriceUpdate] = []
        ts_iso = decision_ts.isoformat()
        for asset_id, features in feature_map.items():
            raw = {name: feature.value for name, feature in features.items()}
            missing = [name for name, feature in features.items() if feature.missing]
            raw, masked_forecast = mask_unreliable_forecast(session, raw)
            result = compute_scores(
                raw,
                missing,
                data_layer_uncertainty=self._rpc_pool_uncertainty(session, asset_id),
                session=session,
            )
            # Ensemble: blend rule-based with ML/heuristic when available
            try:
                ml_score = raw.get("collapse_probability_24h")
                h_score = raw.get("ignition_signal")
                ensemble = ensemble_engine.blend(
                    rule_hype=result.hype,
                    rule_risk=result.risk,
                    rule_confidence=result.confidence,
                    rule_research_priority=result.research_priority,
                    rule_risk_band=result.risk_band.value,
                    ml_hype=clamp(100.0 - ml_score * 100) if ml_score is not None else None,
                    ml_risk=clamp(ml_score * 100) if ml_score is not None else None,
                    heuristic_score=h_score if h_score is not None else None,
                )
                result = _replace(
                    result,
                    hype=ensemble.hype,
                    risk=ensemble.risk,
                    confidence=ensemble.confidence,
                    research_priority=ensemble.research_priority,
                )
            except Exception:  # noqa: BLE001
                pass  # ensemble is additive, never breaks scoring
            score = self._upsert_score(
                session, asset_id=asset_id, decision_ts=decision_ts, result=result
            )
            self._upsert_explanation(
                session,
                score=score,
                result=result,
                changed_features=self._changed_features(
                    session, asset_id=asset_id, decision_ts=decision_ts, current=raw
                ),
            )
            self._maybe_create_alert(session, score=score, result=result)
            self._record_risk_outcome(
                session,
                asset_id=asset_id,
                score=score,
                decision_ts=decision_ts,
            )
            scores.append(score)
        # Batch-query latest market + liquidity snapshots for all scored assets
        # (2 queries total, not N+1), then build PriceUpdate objects.
        scored_ids = list(feature_map.keys())
        if scored_ids:
            price_updates = self._build_price_updates(session, scored_ids, ts_iso)
            for update in price_updates:
                broadcast_price(update)
        return scores

    def _build_price_updates(
        self,
        session: Session,
        asset_ids: list[int],
        ts_iso: str,
    ) -> list[PriceUpdate]:
        """Batch-fetch latest market and liquidity data for scored assets.

        Two bulk queries replace what would otherwise be N+1 per-asset queries.
        """
        # Latest market snapshot per base_asset_id
        latest_market_sub = (
            select(
                models.MarketSnapshot.pair_id,
                models.MarketSnapshot.price_usd,
                models.MarketSnapshot.volume_usd,
                func.row_number()
                .over(
                    partition_by=models.MarketSnapshot.pair_id,
                    order_by=models.MarketSnapshot.ts.desc(),
                )
                .label("rn"),
            )
            .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
            .where(models.Pair.base_asset_id.in_(asset_ids))
            .subquery()
        )
        market_stmt = (
            select(
                models.Pair.base_asset_id,
                latest_market_sub.c.price_usd,
                latest_market_sub.c.volume_usd,
            )
            .join(models.Pair, models.Pair.id == latest_market_sub.c.pair_id)
            .where(latest_market_sub.c.rn == 1)
        )
        market_rows = session.execute(market_stmt).all()
        price_by_asset: dict[int, tuple[float | None, float | None]] = {}
        for row in market_rows:
            price_by_asset[row.base_asset_id] = (row.price_usd, row.volume_usd)

        # Latest liquidity snapshot per pool via pair
        latest_liq_sub = select(
            models.LiquiditySnapshot.pool_id,
            models.LiquiditySnapshot.reserve_usd,
            func.row_number()
            .over(
                partition_by=models.LiquiditySnapshot.pool_id,
                order_by=models.LiquiditySnapshot.ts.desc(),
            )
            .label("rn"),
        ).subquery()
        liq_stmt = (
            select(
                models.Pair.base_asset_id,
                latest_liq_sub.c.reserve_usd,
            )
            .join(models.Pool, models.Pool.id == latest_liq_sub.c.pool_id)
            .join(models.Pair, models.Pair.pool_id == models.Pool.id)
            .where(
                models.Pair.base_asset_id.in_(asset_ids),
                latest_liq_sub.c.rn == 1,
            )
        )
        liq_rows = session.execute(liq_stmt).all()
        liq_by_asset: dict[int, float | None] = {}
        for row in liq_rows:
            liq_by_asset[row.base_asset_id] = row.reserve_usd

        updates: list[PriceUpdate] = []
        for asset_id in asset_ids:
            asset = session.get(models.Asset, asset_id)
            if not asset:
                continue
            chain = session.get(models.Chain, asset.chain_id)
            price_vol = price_by_asset.get(asset_id)
            updates.append(
                PriceUpdate(
                    asset_id=asset_id,
                    symbol=asset.symbol,
                    chain=chain.slug if chain else "unknown",
                    address=asset.address,
                    price_usd=price_vol[0] if price_vol else None,
                    volume_usd=price_vol[1] if price_vol else None,
                    liquidity_usd=liq_by_asset.get(asset_id),
                    timestamp=ts_iso,
                )
            )
        return updates

    def _upsert_score(
        self, session: Session, *, asset_id: int, decision_ts: datetime, result
    ) -> models.Score:
        row = session.scalar(
            select(models.Score).where(
                models.Score.asset_id == asset_id,
                models.Score.decision_ts == decision_ts,
                models.Score.model_version == self.settings.model_version,
            )
        )
        values = {
            "hype": result.hype,
            "ethos": result.ethos,
            "risk": result.risk,
            "liquidity_access": result.liquidity_access,
            "manipulation": result.manipulation,
            "confidence": result.confidence,
            "uncertainty": result.uncertainty,
            "catalyst": result.catalyst,
            "exit_risk": result.exit_risk,
            "research_priority": result.research_priority,
            "risk_band": result.risk_band.value,
            "observed_at": utc_now(),
        }
        if row:
            for key, value in values.items():
                setattr(row, key, value)
            return row
        row = models.Score(
            asset_id=asset_id,
            decision_ts=decision_ts,
            model_version=self.settings.model_version,
            **values,
        )
        session.add(row)
        session.flush()
        return row

    def _upsert_explanation(
        self,
        session: Session,
        *,
        score: models.Score,
        result,
        changed_features: dict[str, dict[str, float]],
    ) -> models.ScoreExplanation:
        row = session.scalar(
            select(models.ScoreExplanation).where(models.ScoreExplanation.score_id == score.id)
        )
        payload = {
            "drivers": result.drivers,
            "risk_reasons": result.risk_reasons,
            "missing_features": result.missing_features,
            "changed_features": changed_features,
        }
        if row:
            row.drivers = payload["drivers"]
            row.risk_reasons = payload["risk_reasons"]
            row.missing_features = payload["missing_features"]
            row.changed_features = payload["changed_features"]
            return row
        row = models.ScoreExplanation(score_id=score.id, **payload)
        session.add(row)
        session.flush()
        return row

    def _changed_features(
        self,
        session: Session,
        *,
        asset_id: int,
        decision_ts: datetime,
        current: dict[str, float],
    ) -> dict[str, dict[str, float]]:
        previous_ts = session.scalar(
            select(func.max(models.Feature.decision_ts)).where(
                models.Feature.asset_id == asset_id,
                models.Feature.decision_ts < decision_ts,
            )
        )
        if previous_ts is None:
            return {}
        previous = {
            row.feature_name: row.feature_value
            for row in session.scalars(
                select(models.Feature).where(
                    models.Feature.asset_id == asset_id,
                    models.Feature.decision_ts == previous_ts,
                    models.Feature.missing_flag.is_(False),
                )
            )
        }
        changed: dict[str, dict[str, float]] = {}
        for name, current_value in current.items():
            if name not in previous:
                continue
            prior_value = float(previous[name])
            delta = float(current_value) - prior_value
            scale = max(1.0, abs(prior_value), abs(float(current_value)))
            if abs(delta) / scale < 0.01:
                continue
            changed[name] = {
                "previous": round(prior_value, 6),
                "current": round(float(current_value), 6),
                "delta": round(delta, 6),
                "pct_delta": round((delta / prior_value) * 100.0, 4) if prior_value != 0 else 0.0,
            }
        return dict(
            sorted(changed.items(), key=lambda item: abs(item[1]["delta"]), reverse=True)[:12]
        )

    def _maybe_create_alert(self, session: Session, *, score: models.Score, result) -> None:
        alert_type: AlertType | None = None
        message: str | None = None
        if result.risk_band == RiskBand.BLACK and result.hype >= 50:
            alert_type = AlertType.RED_RISK_HYPE
            message = "Hype is elevated but risk engine produced BLACK hard reject."
        elif result.research_priority >= 40:
            alert_type = AlertType.NEW_INTERESTING_TOKEN
            message = "Research priority crossed MVP threshold."
        elif result.exit_risk >= 75:
            alert_type = AlertType.THESIS_INVALIDATED
            message = "Exit risk is severely elevated."
        if not alert_type or not message:
            return
        existing = session.scalar(
            select(models.Alert).where(
                models.Alert.asset_id == score.asset_id,
                models.Alert.score_id == score.id,
                models.Alert.alert_type == alert_type.value,
            )
        )
        if existing:
            return
        from ops.alert_quality import alert_generation_allowed

        if not alert_generation_allowed(session, alert_type.value, self.settings):
            return
        session.add(
            models.Alert(
                asset_id=score.asset_id,
                score_id=score.id,
                alert_type=alert_type.value,
                threshold_version=self.settings.model_version,
                score_snapshot_ref=f"score:{score.id}",
                state=AlertState.OPEN.value,
                message=message,
            )
        )

    def _record_risk_outcome(
        self,
        session: Session,
        *,
        asset_id: int,
        score: models.Score,
        decision_ts: datetime,
    ) -> None:
        """Record a risk outcome for adaptive calibration.

        Only records outcomes for non-GREEN bands so the calibrator has
        meaningful signal to learn from (GREEN tokens are expected to be safe).
        """
        if score.risk_band == RiskBand.GREEN.value:
            return
        try:
            from risk_engine.outcomes import record_risk_outcome

            record_risk_outcome(
                session,
                asset_id=asset_id,
                risk_band=score.risk_band,
                score_id=score.id,
                decision_ts=decision_ts,
            )
        except Exception as exc:  # noqa: BLE001 - outcome recording must not break scoring.
            log.warning("risk_outcome_recording_failed", error=str(exc), asset_id=asset_id)


def score_current_assets(
    session: Session, *, decision_ts: datetime | None = None, asset_ids: list[int] | None = None
) -> list[models.Score]:
    engine = ScoringEngine()
    return engine.score_assets(session, decision_ts=decision_ts, asset_ids=asset_ids)
