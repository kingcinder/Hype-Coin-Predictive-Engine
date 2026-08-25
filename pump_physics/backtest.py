"""Walk-forward backtest for the hype-lifecycle state machine.

Replays the lifecycle scanner step-by-step across history using persisted
point-in-time evidence only (``observed_at <= decision_ts``), maintaining the
monotonic phase per asset *in memory* — the same discipline as the production
scan, but with no reliance on persisted ``lifecycle_events`` rows, which could
include future knowledge. Every transition into a measured phase is evaluated
against realized forward prices:

- IGNITION / PARABOLIC transitions predict a **pump**: the price must cross
  ``+ignition_return_pct`` inside the forward window (lead time = minutes to the
  first crossing), otherwise the transition is a false alarm.
- SATURATION / COLLAPSE transitions predict a **collapse**: the price must cross
  ``collapse_return_pct`` inside the forward window, otherwise a false alarm.

SEEDING has no outcome to measure, and the terminal exits (DEAD / RUGGED /
SURVIVOR) are classifications of evidence rather than price predictions, so
they are not scored here. Metrics are persisted per transition type on a
``BacktestRun`` (``model_version=lifecycle_model_version``) so the Backtest &
Drift UI shows them next to the rule-based backtests.

Run::

    python -m pump_physics.backtest --start 2026-05-01T00:00:00Z --end 2026-06-01T00:00:00Z
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import LifecyclePhase
from common.time import ensure_utc, floor_to_hour, utc_now
from pump_physics.engine import LifecycleEngine, detect_phase
from storage import models

# Which phases predict which outcome (per transition type).
IGNITION_PHASES = {LifecyclePhase.IGNITION, LifecyclePhase.PARABOLIC}
COLLAPSE_PHASES = {LifecyclePhase.SATURATION, LifecyclePhase.COLLAPSE}


@dataclass(frozen=True)
class LifecycleBacktestConfig:
    start: datetime
    end: datetime
    step_hours: int = 6
    forward_hours: int = 48
    ignition_return_pct: float = 20.0
    collapse_return_pct: float = -70.0


@dataclass
class _Tally:
    transitions: int = 0
    true_positives: int = 0
    false_alarms: int = 0
    lead_minutes: list[float] = field(default_factory=list)

    def precision(self) -> float:
        return self.true_positives / self.transitions if self.transitions else 0.0

    def false_alarm_rate(self) -> float:
        return self.false_alarms / self.transitions if self.transitions else 0.0

    def median_lead(self) -> float:
        return median(self.lead_minutes) if self.lead_minutes else 0.0


def _stepped_times(start: datetime, end: datetime, step_hours: int) -> list[datetime]:
    current = floor_to_hour(start)
    end = floor_to_hour(end)
    out: list[datetime] = []
    while current <= end:
        out.append(current)
        current += timedelta(hours=max(1, step_hours))
    return out


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:  # noqa: BLE001 - not a git checkout; None is honest.
        return None


def _entry_price(session: Session, *, asset_id: int, decision_ts: datetime) -> float | None:
    pair_ids = session.scalars(
        select(models.Pair.id).where(models.Pair.base_asset_id == asset_id)
    ).all()
    if not pair_ids:
        return None
    row = session.scalar(
        select(models.MarketSnapshot)
        .where(
            models.MarketSnapshot.pair_id.in_(pair_ids),
            models.MarketSnapshot.ts <= decision_ts,
            models.MarketSnapshot.observed_at <= decision_ts,
            models.MarketSnapshot.price_usd.is_not(None),
        )
        .order_by(models.MarketSnapshot.ts.desc())
        .limit(1)
    )
    if row is None or row.price_usd is None or row.price_usd <= 0:
        return None
    return float(row.price_usd)


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


def _crossing_minutes(
    rows: list[tuple[datetime, float]],
    decision_ts: datetime,
    entry: float,
    target_pct: float,
) -> float | None:
    """Minutes from the transition until the price first crosses the target."""
    threshold = entry * (1.0 + target_pct / 100.0)
    for ts, price in rows:
        if (target_pct >= 0 and price >= threshold) or (target_pct < 0 and price <= threshold):
            return max(0.0, (ts - decision_ts).total_seconds() / 60.0)
    return None


class LifecycleBacktestRunner:
    """Walk-forward replay of the lifecycle state machine with outcome scoring."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.engine = LifecycleEngine()

    def run(self, session: Session, config: LifecycleBacktestConfig) -> models.BacktestRun:
        start = ensure_utc(config.start)
        end = ensure_utc(config.end)
        run = models.BacktestRun(
            cutoff_start=start,
            cutoff_end=end,
            config_json={
                "step_hours": config.step_hours,
                "forward_hours": config.forward_hours,
                "ignition_return_pct": config.ignition_return_pct,
                "collapse_return_pct": config.collapse_return_pct,
            },
            git_sha=_git_sha(),
            model_version=self.settings.lifecycle_model_version,
            status="running",
        )
        session.add(run)
        session.flush()

        ignition = _Tally()
        collapse = _Tally()
        assets_with_transitions: set[int] = set()
        unevaluated = 0
        steps = 0
        current_phase: dict[int, LifecyclePhase] = {}

        for decision_ts in _stepped_times(start, end, config.step_hours):
            steps += 1
            for asset in self._assets(session):
                evidence = self.engine._evidence(session, asset, decision_ts)
                if evidence is None:
                    continue
                detected = detect_phase(evidence)
                if not self.engine._advances(current_phase.get(asset.id), detected):
                    continue
                if detected in IGNITION_PHASES:
                    tally, target = ignition, config.ignition_return_pct
                elif detected in COLLAPSE_PHASES:
                    tally, target = collapse, config.collapse_return_pct
                else:
                    # SEEDING and the terminal exits are not price predictions.
                    current_phase[asset.id] = detected
                    continue
                current_phase[asset.id] = detected
                assets_with_transitions.add(asset.id)
                if not self._evaluate(
                    session,
                    asset=asset,
                    phase=detected,
                    decision_ts=decision_ts,
                    config=config,
                    target_pct=target,
                    tally=tally,
                ):
                    unevaluated += 1

        self._persist_metrics(
            session,
            run=run,
            ignition=ignition,
            collapse=collapse,
            assets_with_transitions=len(assets_with_transitions),
            steps=steps,
            unevaluated=unevaluated,
        )
        run.status = "completed"
        session.flush()
        return run

    def _assets(self, session: Session):
        return session.scalars(
            select(models.Asset)
            .outerjoin(models.Pair, models.Pair.base_asset_id == models.Asset.id)
            .outerjoin(models.SocialMention, models.SocialMention.asset_id == models.Asset.id)
            .where((models.Pair.id.is_not(None)) | (models.SocialMention.id.is_not(None)))
            .distinct()
        ).all()

    def _evaluate(
        self,
        session: Session,
        *,
        asset: models.Asset,
        phase: LifecyclePhase,
        decision_ts: datetime,
        config: LifecycleBacktestConfig,
        target_pct: float,
        tally: _Tally,
    ) -> bool:
        """Score one transition against realized forward prices.

        Returns True when the transition was evaluated (had an entry price);
        False when there was no market evidence to measure against.
        """
        entry = _entry_price(session, asset_id=asset.id, decision_ts=decision_ts)
        if entry is None:
            return False
        future = _future_rows(
            session,
            asset_id=asset.id,
            start_ts=decision_ts,
            end_ts=decision_ts + timedelta(hours=config.forward_hours),
        )
        lead = _crossing_minutes(future, decision_ts, entry, target_pct)
        tally.transitions += 1
        if lead is not None:
            tally.true_positives += 1
            tally.lead_minutes.append(lead)
        else:
            tally.false_alarms += 1
        return True

    def _persist_metrics(
        self,
        session: Session,
        *,
        run: models.BacktestRun,
        ignition: _Tally,
        collapse: _Tally,
        assets_with_transitions: int,
        steps: int,
        unevaluated: int,
    ) -> None:
        metrics: dict[str, float] = {
            "lifecycle.decision_steps": float(steps),
            "lifecycle.assets_with_transitions": float(assets_with_transitions),
            "lifecycle.unevaluated_transitions": float(unevaluated),
            "lifecycle.ignition.transitions": float(ignition.transitions),
            "lifecycle.ignition.true_positives": float(ignition.true_positives),
            "lifecycle.ignition.false_alarms": float(ignition.false_alarms),
            "lifecycle.ignition.precision": round(ignition.precision(), 4),
            "lifecycle.ignition.false_alarm_rate": round(ignition.false_alarm_rate(), 4),
            "lifecycle.ignition.median_lead_minutes": round(ignition.median_lead(), 2),
            "lifecycle.collapse.transitions": float(collapse.transitions),
            "lifecycle.collapse.true_positives": float(collapse.true_positives),
            "lifecycle.collapse.false_alarms": float(collapse.false_alarms),
            "lifecycle.collapse.precision": round(collapse.precision(), 4),
            "lifecycle.collapse.false_alarm_rate": round(collapse.false_alarm_rate(), 4),
            "lifecycle.collapse.median_lead_minutes": round(collapse.median_lead(), 2),
        }
        combined = ignition.transitions + collapse.transitions
        combined_tp = ignition.true_positives + collapse.true_positives
        metrics["lifecycle.overall.precision"] = (
            round(combined_tp / combined, 4) if combined else 0.0
        )
        metrics["lifecycle.overall.false_alarm_rate"] = (
            round(1.0 - combined_tp / combined, 4) if combined else 0.0
        )
        metrics["lifecycle.overall.median_ignition_lead_minutes"] = round(ignition.median_lead(), 2)
        metrics["lifecycle.overall.median_collapse_lead_minutes"] = round(collapse.median_lead(), 2)
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


def run_lifecycle_backtest(
    session: Session,
    *,
    start: datetime,
    end: datetime | None = None,
    step_hours: int = 6,
    forward_hours: int = 48,
) -> models.BacktestRun:
    return LifecycleBacktestRunner().run(
        session,
        LifecycleBacktestConfig(
            start=start,
            end=end or utc_now(),
            step_hours=step_hours,
            forward_hours=forward_hours,
        ),
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Walk-forward lifecycle state-machine backtest")
    parser.add_argument("--start", required=True, help="ISO datetime, e.g. 2026-05-01T00:00:00Z")
    parser.add_argument("--end", help="ISO datetime (default: now)")
    parser.add_argument("--step-hours", type=int, default=6)
    parser.add_argument("--forward-hours", type=int, default=48)
    args = parser.parse_args()

    from storage.database import SessionLocal

    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else None
    with SessionLocal() as session:
        run = run_lifecycle_backtest(
            session,
            start=start,
            end=end,
            step_hours=args.step_hours,
            forward_hours=args.forward_hours,
        )
        session.commit()
        metrics = {
            row.metric_name: row.metric_value
            for row in session.scalars(
                select(models.BacktestResult).where(models.BacktestResult.run_id == run.id)
            )
        }
    print(f"lifecycle backtest run {run.id}: {run.status} (git {run.git_sha or 'n/a'})")
    for name, value in sorted(metrics.items()):
        print(f"  {name}: {value:.4f}")


if __name__ == "__main__":
    main()
