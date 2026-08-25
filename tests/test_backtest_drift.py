from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ops.backtest_drift import _compute_drift, run_backtest_autopilot
from storage import models
from tests.conftest import seed_market_asset


def _seed_run(session, *, metrics: dict[str, float], started_at: datetime) -> models.BacktestRun:
    """Insert a completed BacktestRun with the given headline metrics."""
    run = models.BacktestRun(
        cutoff_start=started_at - timedelta(days=7),
        cutoff_end=started_at,
        config_json={},
        git_sha=None,
        model_version="test-v1",
        status="completed",
    )
    session.add(run)
    session.flush()
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
    session.flush()
    return run


def test_compute_drift_ok_without_baseline() -> None:
    state, reasons = _compute_drift(
        {"precision_at_10": 0.5},
        {},
        precision_margin=0.15,
        return_floor=-10.0,
        collapse_rise=0.15,
    )
    assert state == "ok"
    assert reasons == []


def test_compute_drift_ok_with_stable_metrics() -> None:
    state, reasons = _compute_drift(
        {"precision_at_10": 0.45, "median_forward_return": 5.0, "collapse_rate": 0.2},
        {"precision_at_10": 0.50, "median_forward_return": 6.0, "collapse_rate": 0.18},
        precision_margin=0.15,
        return_floor=-10.0,
        collapse_rise=0.15,
    )
    assert state == "ok"
    assert reasons == []


def test_compute_drift_yellow_on_single_slip() -> None:
    state, reasons = _compute_drift(
        {"precision_at_10": 0.30, "median_forward_return": 5.0, "collapse_rate": 0.2},
        {"precision_at_10": 0.50, "median_forward_return": 6.0, "collapse_rate": 0.18},
        precision_margin=0.15,
        return_floor=-10.0,
        collapse_rise=0.15,
    )
    assert state == "yellow"
    assert any("precision@10" in reason for reason in reasons)


def test_compute_drift_red_on_return_floor_breach() -> None:
    state, reasons = _compute_drift(
        {"precision_at_10": 0.30, "median_forward_return": -12.0, "collapse_rate": 0.2},
        {"precision_at_10": 0.50, "median_forward_return": 6.0, "collapse_rate": 0.18},
        precision_margin=0.15,
        return_floor=-10.0,
        collapse_rise=0.15,
    )
    assert state == "red"
    assert any("floor" in reason for reason in reasons)


def test_compute_drift_red_on_multiple_slips() -> None:
    state, reasons = _compute_drift(
        {"precision_at_10": 0.20, "median_forward_return": 5.0, "collapse_rate": 0.5},
        {"precision_at_10": 0.50, "median_forward_return": 6.0, "collapse_rate": 0.2},
        precision_margin=0.15,
        return_floor=-10.0,
        collapse_rise=0.15,
    )
    assert state == "red"
    assert len(reasons) >= 2


def test_run_backtest_autopilot_records_health_and_alert(session, monkeypatch) -> None:
    """A red drift run records a SystemHealth row and opens a drift Alert."""
    seed_market_asset(session)
    baseline_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    baseline = _seed_run(
        session,
        metrics={
            "precision_at_10": 0.50,
            "median_forward_return": 6.0,
            "collapse_rate": 0.18,
            "scam_avoidance_rate": 0.9,
        },
        started_at=baseline_ts,
    )

    def _fake_run_backtest(session_, *, start, end=None, **kwargs):
        return _seed_run(
            session_,
            metrics={
                "precision_at_10": 0.20,
                "median_forward_return": -15.0,
                "collapse_rate": 0.5,
                "scam_avoidance_rate": 0.9,
            },
            started_at=end or baseline_ts + timedelta(hours=24),
        )

    monkeypatch.setattr("ops.backtest_drift.run_backtest", _fake_run_backtest)

    result = run_backtest_autopilot(session)
    session.commit()

    assert result["state"] == "red"
    assert result["baseline_run_id"] == baseline.id
    assert any("floor" in reason for reason in result["reasons"])

    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "backtest_drift")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert health.state == "red"
    assert "backtest run=" in health.message

    alert = session.scalar(select(models.Alert).where(models.Alert.alert_type == "backtest_drift"))
    assert alert is not None
    assert alert.state == "open"
    assert "precision@10" in alert.message


def test_run_backtest_autopilot_ok_records_health_only(session, monkeypatch) -> None:
    """An ok run records health but opens no alert."""
    seed_market_asset(session)
    baseline_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _seed_run(
        session,
        metrics={
            "precision_at_10": 0.50,
            "median_forward_return": 6.0,
            "collapse_rate": 0.18,
        },
        started_at=baseline_ts,
    )

    def _fake_run_backtest(session_, *, start, end=None, **kwargs):
        return _seed_run(
            session_,
            metrics={
                "precision_at_10": 0.48,
                "median_forward_return": 5.5,
                "collapse_rate": 0.20,
            },
            started_at=end or baseline_ts + timedelta(hours=24),
        )

    monkeypatch.setattr("ops.backtest_drift.run_backtest", _fake_run_backtest)

    result = run_backtest_autopilot(session)
    session.commit()

    assert result["state"] == "ok"
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "backtest_drift")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert health.state == "ok"
    assert "no drift" in health.message
    alert = session.scalar(select(models.Alert).where(models.Alert.alert_type == "backtest_drift"))
    assert alert is None
