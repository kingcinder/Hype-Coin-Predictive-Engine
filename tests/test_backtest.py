from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from backtest.runner import BacktestConfig, point_in_time_market_rows, run_backtest
from common.config import get_settings
from common.time import ensure_utc
from storage import models
from storage.repository import insert_market_snapshot_once
from tests.conftest import seed_market_asset


def test_point_in_time_market_rows_exclude_late_arrivals(session) -> None:
    asset = seed_market_asset(session)
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    decision = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=decision - timedelta(minutes=30),
        observed_at=decision + timedelta(hours=1),
        price_usd=999.0,
        volume_usd=999.0,
    )
    session.commit()
    rows = point_in_time_market_rows(session, asset_id=asset.id, decision_ts=decision)
    assert all(ensure_utc(row.observed_at) <= decision for row in rows)
    assert all(row.price_usd != 999.0 for row in rows)


def test_backtest_run_writes_metrics(session) -> None:
    seed_market_asset(session)
    run = run_backtest(
        session,
        start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        top_k=10,
        forward_hours=1,
    )
    session.commit()
    assert run.status == "completed"
    metrics = session.scalars(
        select(models.BacktestResult).where(models.BacktestResult.run_id == run.id)
    ).all()
    names = {metric.metric_name for metric in metrics}
    assert names >= {
        "precision_at_10",
        "median_forward_return",
        "median_ignition_lead_minutes",
        "median_collapse_warning_lead_minutes",
        "false_alarm_rate",
    }
    assert run.git_sha is None or len(run.git_sha) == 40


def test_backtest_threads_feature_source_into_scoring(session, monkeypatch) -> None:
    """feature_source flows from the config through the runner into
    build_and_persist_features and is recorded on the run for audit."""
    import scoring.engine as scoring_engine

    captured: dict[str, str] = {}

    def _fake_build(
        session, *, decision_ts=None, asset_ids=None, feature_source="sql"
    ):
        captured["feature_source"] = feature_source
        return {}

    monkeypatch.setattr(scoring_engine, "build_and_persist_features", _fake_build)
    seed_market_asset(session)
    run = run_backtest(
        session,
        start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        top_k=10,
        forward_hours=1,
        feature_source="lake",
    )
    session.commit()

    assert captured["feature_source"] == "lake"
    assert run.status == "completed"
    assert run.config_json["feature_source"] == "lake"
    assert BacktestConfig(
        start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    ).feature_source == "sql"


def test_backtest_surfaces_latest_forecast_metrics(session) -> None:
    """The walk-forward backtest reports the latest forecast training's blended
    and real-only metrics alongside its own scoring metrics, so both readings
    (including the real-only ones dense labels could mask) are visible."""
    seed_market_asset(session)
    forecast_run = models.BacktestRun(
        cutoff_start=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        cutoff_end=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        config_json={},
        git_sha=None,
        model_version=get_settings().forecast_model_version,
        status="completed",
    )
    session.add(forecast_run)
    session.flush()
    for name, value in {
        "forecast.precision_at_10": 0.5,
        "forecast.calibration_error": 0.12,
        "forecast.precision_at_10_real": 0.3,
        "forecast.calibration_error_real": 0.31,
        "forecast.real_test_samples": 8.0,
        "forecast.test_samples": 40.0,
    }.items():
        session.add(
            models.BacktestResult(
                run_id=forecast_run.id,
                metric_name=name,
                metric_value=float(value),
                chain_slug=None,
                details_json={},
            )
        )
    session.commit()

    run = run_backtest(
        session,
        start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        top_k=10,
        forward_hours=1,
    )
    session.commit()
    metrics = {
        metric.metric_name: metric.metric_value
        for metric in session.scalars(
            select(models.BacktestResult).where(models.BacktestResult.run_id == run.id)
        ).all()
    }
    assert metrics.get("forecast.precision_at_10") == pytest.approx(0.5)
    assert metrics.get("forecast.calibration_error") == pytest.approx(0.12)
    assert metrics.get("forecast.precision_at_10_real") == pytest.approx(0.3)
    assert metrics.get("forecast.calibration_error_real") == pytest.approx(0.31)
    assert metrics.get("forecast.real_test_samples") == pytest.approx(8.0)
    assert metrics.get("forecast.test_samples") == pytest.approx(40.0)
    # The backtest's own scoring metric is still present alongside them.
    assert "precision_at_10" in metrics
