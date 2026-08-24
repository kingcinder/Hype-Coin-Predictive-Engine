from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.time import ensure_utc, hours_between, utc_now
from features.lake import LakeFeatureFactory
from scoring.engine import ScoringEngine
from storage import models

# Blended + real-only forecast metrics surfaced from the most recent forecast
# training run onto each scoring backtest run, so walk-forward output reports
# both readings. The real-only names are what dense-label interpolation could
# otherwise mask, so exposing them alongside the scoring metrics keeps the
# operator honest about whether the forecast layer works on observed outcomes.
_FORECAST_METRIC_NAMES = (
    "forecast.precision_at_10",
    "forecast.calibration_error",
    "forecast.precision_at_10_real",
    "forecast.calibration_error_real",
    "forecast.real_test_samples",
    "forecast.test_samples",
)


@dataclass(frozen=True)
class BacktestConfig:
    start: datetime
    end: datetime
    top_k: int = 10
    forward_hours: int = 24
    min_forward_return_pct: float = 20.0
    collapse_return_pct: float = -70.0
    feature_source: str = "sql"  # "sql" (live tables) or "lake" (archived lake replay)


def point_in_time_market_rows(
    session: Session, *, asset_id: int, decision_ts: datetime
) -> list[models.MarketSnapshot]:
    pair_ids = session.scalars(
        select(models.Pair.id).where(models.Pair.base_asset_id == asset_id)
    ).all()
    if not pair_ids:
        return []
    return list(
        session.scalars(
            select(models.MarketSnapshot)
            .where(
                models.MarketSnapshot.pair_id.in_(pair_ids),
                models.MarketSnapshot.ts <= decision_ts,
                models.MarketSnapshot.observed_at <= decision_ts,
            )
            .order_by(models.MarketSnapshot.ts)
        )
    )


def _latest_price_at(session: Session, *, asset_id: int, decision_ts: datetime) -> float | None:
    rows = point_in_time_market_rows(session, asset_id=asset_id, decision_ts=decision_ts)
    rows = [row for row in rows if row.price_usd and row.price_usd > 0]
    if not rows:
        return None
    price = rows[-1].price_usd
    return float(price) if price is not None else None


def _future_rows(
    session: Session, *, asset_id: int, start_ts: datetime, end_ts: datetime
) -> list[tuple[datetime, float]]:
    pair_ids = session.scalars(
        select(models.Pair.id).where(models.Pair.base_asset_id == asset_id)
    ).all()
    if not pair_ids:
        return []
    rows = session.scalars(
        select(models.MarketSnapshot)
        .where(
            models.MarketSnapshot.pair_id.in_(pair_ids),
            models.MarketSnapshot.ts > start_ts,
            models.MarketSnapshot.ts <= end_ts,
            models.MarketSnapshot.observed_at <= end_ts,
            models.MarketSnapshot.price_usd.is_not(None),
        )
        .order_by(models.MarketSnapshot.ts)
    ).all()
    return [
        (ensure_utc(row.ts), float(row.price_usd))
        for row in rows
        if row.price_usd is not None and row.price_usd > 0
    ]


def _future_prices(
    session: Session, *, asset_id: int, start_ts: datetime, end_ts: datetime
) -> list[float]:
    return [
        price
        for _, price in _future_rows(
            session, asset_id=asset_id, start_ts=start_ts, end_ts=end_ts
        )
    ]


def _lead_time_minutes(
    rows: list[tuple[datetime, float]], decision_ts: datetime, entry: float, target_pct: float
) -> float | None:
    """Minutes from the flag until the price first crosses ``target_pct``."""
    threshold = entry * (1.0 + target_pct / 100.0)
    for ts, price in rows:
        if (target_pct >= 0 and price >= threshold) or (
            target_pct < 0 and price <= threshold
        ):
            return max(0.0, (ts - decision_ts).total_seconds() / 60.0)
    return None


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:  # noqa: BLE001 - not a git checkout; None is honest.
        return None


def _latest_forecast_metrics(session: Session) -> dict[str, float]:
    """The most recent forecast training run's blended + real-only metrics."""
    run = session.scalar(
        select(models.BacktestRun)
        .where(
            models.BacktestRun.model_version == get_settings().forecast_model_version,
            models.BacktestRun.status == "completed",
        )
        .order_by(models.BacktestRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        return {}
    rows = session.execute(
        select(models.BacktestResult.metric_name, models.BacktestResult.metric_value)
        .where(
            models.BacktestResult.run_id == run.id,
            models.BacktestResult.metric_name.in_(_FORECAST_METRIC_NAMES),
        )
    ).all()
    return {str(name): float(value) for name, value in rows}


class BacktestRunner:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.scoring = ScoringEngine()

    def run(self, session: Session, config: BacktestConfig) -> models.BacktestRun:
        start = ensure_utc(config.start)
        end = ensure_utc(config.end)
        run = models.BacktestRun(
            cutoff_start=start,
            cutoff_end=end,
            config_json={
                "top_k": config.top_k,
                "forward_hours": config.forward_hours,
                "min_forward_return_pct": config.min_forward_return_pct,
                "collapse_return_pct": config.collapse_return_pct,
                "feature_source": config.feature_source,
            },
            git_sha=_git_sha(),
            model_version=self.settings.model_version,
            status="running",
        )
        session.add(run)
        session.flush()

        returns: list[float] = []
        drawdowns: list[float] = []
        ignition_lead_minutes: list[float] = []
        collapse_warning_lead_minutes: list[float] = []
        collapses = 0
        flagged = 0
        selected = 0
        useful = 0
        scam_avoided = 0
        total_decisions = 0
        # Snapshot the lake reconstruction cache before the walk-forward so the
        # run can report how many DuckDB queries the (asset, hour) cache saved
        # (replay runs over a warm cache hit instead of re-querying DuckDB).
        lake_cache_before = (
            LakeFeatureFactory.cache_stats() if config.feature_source == "lake" else None
        )

        for decision_ts in hours_between(start, end):
            asset_ids = [
                asset.id
                for asset in session.scalars(
                    select(models.Asset).where(models.Asset.first_seen_at <= decision_ts)
                ).all()
            ]
            if not asset_ids:
                continue
            self.scoring.score_assets(
                session,
                decision_ts=decision_ts,
                asset_ids=asset_ids,
                feature_source=config.feature_source,
            )
            session.flush()
            candidates = session.scalars(
                select(models.Score)
                .where(
                    models.Score.decision_ts == decision_ts,
                    models.Score.model_version == self.settings.model_version,
                    models.Score.risk_band != "BLACK",
                )
                .order_by(desc(models.Score.research_priority))
                .limit(config.top_k)
            ).all()
            if not candidates:
                continue
            total_decisions += 1
            for score in candidates:
                selected += 1
                entry = _latest_price_at(session, asset_id=score.asset_id, decision_ts=decision_ts)
                future = _future_prices(
                    session,
                    asset_id=score.asset_id,
                    start_ts=decision_ts,
                    end_ts=decision_ts + timedelta(hours=config.forward_hours),
                )
                if not entry or not future:
                    continue
                final_return = (future[-1] / entry - 1.0) * 100.0
                min_return = (min(future) / entry - 1.0) * 100.0
                returns.append(final_return)
                drawdowns.append(min_return)
                flagged += 1
                future_rows = _future_rows(
                    session,
                    asset_id=score.asset_id,
                    start_ts=decision_ts,
                    end_ts=decision_ts + timedelta(hours=config.forward_hours),
                )
                pump_lead = _lead_time_minutes(
                    future_rows,
                    decision_ts,
                    entry,
                    config.min_forward_return_pct,
                )
                if pump_lead is not None:
                    ignition_lead_minutes.append(pump_lead)
                collapse_lead = _lead_time_minutes(
                    future_rows, decision_ts, entry, config.collapse_return_pct
                )
                if collapse_lead is not None:
                    collapse_warning_lead_minutes.append(collapse_lead)
                if final_return >= config.min_forward_return_pct:
                    useful += 1
                if min_return <= config.collapse_return_pct:
                    collapses += 1
                if score.risk_band in {"GREEN", "YELLOW"}:
                    scam_avoided += 1

        metrics = {
            "precision_at_10": useful / selected if selected else 0.0,
            "median_forward_return": median(returns) if returns else 0.0,
            "max_drawdown_after_alert": min(drawdowns) if drawdowns else 0.0,
            "collapse_rate": collapses / selected if selected else 0.0,
            "scam_avoidance_rate": scam_avoided / selected if selected else 0.0,
            "median_ignition_lead_minutes": (
                median(ignition_lead_minutes) if ignition_lead_minutes else 0.0
            ),
            "median_collapse_warning_lead_minutes": (
                median(collapse_warning_lead_minutes) if collapse_warning_lead_minutes else 0.0
            ),
            "false_alarm_rate": (flagged - collapses) / flagged if flagged else 0.0,
            "alerts_evaluated": float(selected),
            "decision_hours": float(total_decisions),
        }
        if lake_cache_before is not None:
            after = LakeFeatureFactory.cache_stats()
            lake_cache = {key: after[key] - lake_cache_before[key] for key in after}
            metrics["lake_cache.hits"] = float(lake_cache.get("hits", 0))
            metrics["lake_cache.misses"] = float(lake_cache.get("misses", 0))
            metrics["lake_cache.saved_queries"] = float(lake_cache.get("saved_queries", 0))
        for name, value in metrics.items():
            session.add(
                models.BacktestResult(
                    run_id=run.id,
                    metric_name=name,
                    metric_value=float(value),
                    chain_slug=None,
                    details_json={},
                )
            )
        # Surface the latest forecast training's blended + real-only metrics on
        # this run too, so walk-forward output reports both readings (and the
        # real-only ones that dense-label interpolation could mask) next to the
        # scoring metrics.
        for name, value in _latest_forecast_metrics(session).items():
            session.add(
                models.BacktestResult(
                    run_id=run.id,
                    metric_name=name,
                    metric_value=float(value),
                    chain_slug=None,
                    details_json={},
                )
            )
        run.status = "completed"
        session.flush()
        return run


def run_backtest(
    session: Session,
    *,
    start: datetime,
    end: datetime | None = None,
    top_k: int = 10,
    forward_hours: int = 24,
    feature_source: str = "sql",
) -> models.BacktestRun:
    runner = BacktestRunner()
    return runner.run(
        session,
        BacktestConfig(
            start=start,
            end=end or utc_now(),
            top_k=top_k,
            forward_hours=forward_hours,
            feature_source=feature_source,
        ),
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Replay-safe backtest runner")
    parser.add_argument("--start", required=True, help="ISO datetime, e.g. 2026-05-01T10:00:00Z")
    parser.add_argument("--end", help="ISO datetime (default: now)")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--forward-hours", type=int, default=24)
    parser.add_argument(
        "--feature-source",
        choices=["sql", "lake"],
        default="sql",
        help="feature read path: 'sql' (live tables) or 'lake' (archived lake replay)",
    )
    args = parser.parse_args()

    from storage.database import SessionLocal

    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    end = (
        datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else None
    )
    with SessionLocal() as session:
        run = run_backtest(
            session,
            start=start,
            end=end,
            top_k=args.top_k,
            forward_hours=args.forward_hours,
            feature_source=args.feature_source,
        )
        session.commit()
        metrics = {
            row.metric_name: row.metric_value
            for row in session.scalars(
                select(models.BacktestResult).where(models.BacktestResult.run_id == run.id)
            )
        }
    print(f"backtest run {run.id}: {run.status} (git {run.git_sha or 'n/a'})")
    for name, value in sorted(metrics.items()):
        print(f"  {name}: {value:.4f}")


if __name__ == "__main__":
    main()
