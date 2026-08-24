from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from backtest.runner import BacktestConfig, point_in_time_market_rows, run_backtest
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
