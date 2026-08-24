from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import func, select

from common.config import Settings
from forecast.engine import (
    CALIBRATION_GAP_HEALTH_COMPONENT,
    FORECAST_FEATURE_NAMES,
    ForecastEngine,
    Sample,
    _widen_probability,
    check_calibration_gap,
    forecast_due,
)
from forecast.hazard import DiscreteHazardModel
from storage import models
from storage.repository import (
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    record_health,
    upsert_asset,
    upsert_feature,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
FORWARD_HOURS = 24


def _seed_arc(session, *, symbol: str, prices: list[float]) -> models.Asset:
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"Addr{symbol}11111111111111111111111111111111111",
        symbol=symbol,
        name=symbol,
        first_seen_at=T0,
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"Quote{symbol}1111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=T0 - timedelta(days=365),
    )
    pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=f"Pair{symbol}1111111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0,
    )
    for hour, price in enumerate(prices):
        ts = T0 + timedelta(hours=hour)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=1_000,
            buys=10,
            sells=5,
        )
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            reserve_usd=200_000,
        )
    return asset


def _seed_features(
    session, asset: models.Asset, hour: int, *, crash: bool = False
) -> None:
    ts = T0 + timedelta(hours=hour)
    values = {
        "liquidity_depth": 1_000.0 if crash and hour >= 16 else 200_000.0,
        "one_hour_return": 0.0,
        "five_min_return": 0.0,
        "volume_acceleration": 1.0,
        "top_holder_concentration": 0.1,
        "volatility": 5.0,
        "spread_estimate": 1.0,
    }
    if crash:
        # Liquidity starts draining 20h before the collapse and LP is pulled at 16h.
        values["liquidity_change"] = -40.0
        if hour >= 16:
            values["liquidity_withdrawal_signal"] = 1.0
    for name, value in values.items():
        upsert_feature(
            session,
            asset_id=asset.id,
            decision_ts=ts,
            feature_name=name,
            feature_value=value,
            source_count=1,
            freshness_score=1.0,
            missing_flag=False,
        )


def test_hazard_model_estimates_time_to_collapse() -> None:
    model = DiscreteHazardModel(FORWARD_HOURS)
    fit = model.fit(
        times_hours=[10.0, 20.0, 24.0, 24.0, 24.0],
        events=[True, True, True, False, False],
    )
    assert fit.event_count == 3
    assert fit.at_risk_count == 5
    assert fit.mean_time_to_collapse is not None
    assert 0 < fit.mean_time_to_collapse <= FORWARD_HOURS
    assert len(fit.curve) == FORWARD_HOURS + 1


def test_forecast_engine_trains_and_predicts_collapse(session) -> None:
    prices_flat = [1.0] * 49
    prices_crash = [1.0] * 30 + [0.2] * 19
    prices_late_pump = [1.0] * 30 + [2.0] * 19
    flat = _seed_arc(session, symbol="FLAT", prices=prices_flat)
    drop_a = _seed_arc(session, symbol="DROP", prices=prices_crash)
    drop_b = _seed_arc(session, symbol="DROP2", prices=prices_crash)
    late = _seed_arc(session, symbol="LATE", prices=prices_late_pump)

    for asset in (flat, drop_a, drop_b, late):
        for hour in range(0, 25):
            _seed_features(
                session,
                asset,
                hour,
                crash=asset.symbol in ("DROP", "DROP2"),
            )
    session.commit()

    engine = ForecastEngine()
    engine.settings.forecast_min_samples = 5
    decision = T0 + timedelta(hours=48)
    result = engine.run(session, decision_ts=decision)
    session.commit()
    assert result["status"] == "ok"
    assert result["samples"] >= 5
    assert result["forecasts"] == 4

    forecasts = {
        row.asset_id: row
        for row in session.scalars(select(models.Forecast)).all()
    }
    assert set(forecasts.keys()) == {flat.id, drop_a.id, drop_b.id, late.id}
    assert forecasts[drop_a.id].p_collapse_24h > forecasts[flat.id].p_collapse_24h
    assert forecasts[drop_b.id].p_collapse_24h > forecasts[flat.id].p_collapse_24h
    assert forecasts[drop_a.id].calibrated is True
    assert forecasts[drop_a.id].calibration_bucket is not None
    assert forecasts[drop_a.id].expected_hours_to_collapse is not None
    assert any(
        row.expected_hours_to_peak is not None for row in forecasts.values()
    ), "expected_hours_to_peak should populate for ignition-prone assets"

    metrics = session.scalars(
        select(models.BacktestResult).where(
            models.BacktestResult.metric_name == "forecast.precision_at_10"
        )
    ).all()
    assert metrics
    drift_rows = session.scalars(
        select(models.BacktestResult).where(
            models.BacktestResult.metric_name == "forecast.drift.status"
        )
    ).all()
    assert drift_rows
    drift_health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "forecast_drift")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert drift_health is not None
    assert drift_health.state in {"ok", "yellow"}
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "forecast")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert health is not None
    assert health.state == "ok"


def _drift_sample(asset_id: int, ts: datetime) -> Sample:
    return Sample(asset_id=asset_id, ts=ts, features={}, y_ignition=0, y_collapse=0)


def _drift_engine() -> ForecastEngine:
    engine = ForecastEngine()
    engine.settings.forecast_drift_min_samples = 5
    return engine


def _drift_health(session):
    return session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "forecast_drift")
        .order_by(models.SystemHealth.ts.desc())
    )


def test_drift_ok_when_performance_holds(session) -> None:
    engine = _drift_engine()
    decision = datetime(2026, 5, 10, 0, tzinfo=UTC)
    cutoff = decision - timedelta(hours=168)
    probs = [0.95, 0.92, 0.9, 0.88] + [0.1] * 8
    labels = [1, 1, 1, 1] + [0] * 8
    baseline = [
        _drift_sample(1, cutoff - timedelta(hours=1) - timedelta(hours=24 * index))
        for index in range(12)
    ]
    trailing = [_drift_sample(2, decision - timedelta(hours=12 * index)) for index in range(12)]
    result = engine._assess_drift(
        session,
        samples=baseline + trailing,
        probs=np.array(probs + probs),
        labels=np.array(labels + labels),
        decision_ts=decision,
    )
    assert result["status"] == "ok"
    assert _drift_health(session).state == "ok"


def test_drift_detects_trailing_performance_loss(session) -> None:
    engine = _drift_engine()
    decision = datetime(2026, 5, 10, 0, tzinfo=UTC)
    cutoff = decision - timedelta(hours=168)
    baseline_probs = [0.95, 0.92, 0.9, 0.88] + [0.1] * 8
    trailing_probs = [0.9, 0.85, 0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.08, 0.07]
    trailing_labels = [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1]
    baseline = [
        _drift_sample(1, cutoff - timedelta(hours=1) - timedelta(hours=24 * index))
        for index in range(12)
    ]
    trailing = [_drift_sample(2, decision - timedelta(hours=12 * index)) for index in range(12)]
    result = engine._assess_drift(
        session,
        samples=baseline + trailing,
        probs=np.array(baseline_probs + trailing_probs),
        labels=np.array([1, 1, 1, 1] + [0] * 8 + trailing_labels),
        decision_ts=decision,
    )
    assert result["status"] == "drift"
    assert result["measures"]["trailing_precision_at_10"] < 0.25
    assert result["measures"]["baseline_precision_at_10"] == 0.4
    assert "reasons" in result["measures"]
    assert _drift_health(session).state == "yellow"


def test_drift_insufficient_trailing_samples(session) -> None:
    engine = _drift_engine()
    decision = datetime(2026, 5, 10, 0, tzinfo=UTC)
    cutoff = decision - timedelta(hours=168)
    baseline = [
        _drift_sample(1, cutoff - timedelta(hours=1) - timedelta(hours=24)) for _ in range(12)
    ]
    trailing = [_drift_sample(2, decision - timedelta(hours=1)) for _ in range(3)]
    result = engine._assess_drift(
        session,
        samples=baseline + trailing,
        probs=np.array([0.1] * 15),
        labels=np.array([0] * 15),
        decision_ts=decision,
    )
    assert result["status"] == "insufficient_trailing"
    assert _drift_health(session).state == "yellow"


def test_drift_persists_metrics_with_run(session) -> None:
    seed = _drift_engine()
    decision = datetime(2026, 5, 10, 0, tzinfo=UTC)
    cutoff = decision - timedelta(hours=168)
    baseline = [
        _drift_sample(1, cutoff - timedelta(hours=1) - timedelta(hours=24 * index))
        for index in range(12)
    ]
    trailing = [_drift_sample(2, decision - timedelta(hours=12 * index)) for index in range(12)]
    samples = baseline + trailing
    metrics: dict[str, float] = {"samples": float(len(samples))}
    seed._persist_metrics(
        session, samples=samples, decision_ts=decision, metrics=metrics
    )
    session.commit()
    rows = session.scalars(
        select(models.BacktestResult).where(
            models.BacktestResult.metric_name == "forecast.samples"
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].metric_value == 24.0


def test_forecast_training_cadence_uses_persisted_run(session) -> None:
    settings = Settings(
        forecast_enabled=True,
        forecast_train_frequency_hours=24,
        forecast_model_version="cadence-test-v1",
    )
    now = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    assert forecast_due(session, now=now, settings=settings) is True

    session.add(
        models.BacktestRun(
            started_at=now - timedelta(hours=23),
            cutoff_start=now - timedelta(days=2),
            cutoff_end=now - timedelta(hours=23),
            config_json={},
            model_version="cadence-test-v1",
            status="completed",
        )
    )
    session.commit()
    assert forecast_due(session, now=now, settings=settings) is False
    assert forecast_due(
        session, now=now + timedelta(hours=1), settings=settings
    ) is True


def test_forecast_feature_set_includes_velocity_and_rpc_health() -> None:
    names = set(FORECAST_FEATURE_NAMES)
    assert {
        "kol_velocity",
        "github_star_velocity",
        "hf_download_velocity",
        "rpc_pool_health",
    } <= names
    assert _widen_probability(0.9, 0.5) == 0.7
    assert _widen_probability(0.9, 0.0) == 0.5
    assert len(FORECAST_FEATURE_NAMES) == len(set(FORECAST_FEATURE_NAMES))


def test_forecast_feature_set_includes_lifecycle_phase() -> None:
    names = set(FORECAST_FEATURE_NAMES)
    assert "lifecycle_phase" in names
    assert len(FORECAST_FEATURE_NAMES) == len(set(FORECAST_FEATURE_NAMES))


def _seed_velocity_features(
    session,
    asset: models.Asset,
    hour: int,
    *,
    kol: float = 0.0,
    stars: float = 0.0,
    downloads: float = 0.0,
) -> None:
    ts = T0 + timedelta(hours=hour)
    present = kol > 0 or stars > 0 or downloads > 0
    for name, value in (
        ("kol_velocity", kol),
        ("github_star_velocity", stars),
        ("hf_download_velocity", downloads),
    ):
        upsert_feature(
            session,
            asset_id=asset.id,
            decision_ts=ts,
            feature_name=name,
            feature_value=value,
            source_count=1 if present else 0,
            freshness_score=1.0 if present else 0.0,
            missing_flag=not present,
        )


def test_forecast_matrix_carries_velocity_values_and_drift_baseline(session) -> None:
    """The dev-activity proxies reach the training matrix and the drift baseline
    re-persists when they are populated — the forecast feature-set contract."""
    prices_flat = [1.0] * 49
    prices_crash = [1.0] * 30 + [0.2] * 19
    prices_late_pump = [1.0] * 30 + [2.0] * 19
    flat = _seed_arc(session, symbol="FLAT", prices=prices_flat)
    drop_a = _seed_arc(session, symbol="DROP", prices=prices_crash)
    drop_b = _seed_arc(session, symbol="DROP2", prices=prices_crash)
    late = _seed_arc(session, symbol="LATE", prices=prices_late_pump)

    for asset in (flat, drop_a, drop_b, late):
        for hour in range(0, 25):
            _seed_features(
                session, asset, hour, crash=asset.symbol in ("DROP", "DROP2")
            )
            if asset.symbol in ("DROP", "DROP2"):
                # KOL shill + fast-growing repo/model: the dev-activity evidence
                # that should make the hype-mechanics more separable.
                _seed_velocity_features(
                    session, asset, hour, kol=2.0, stars=20.0, downloads=500.0
                )
    session.commit()

    engine = ForecastEngine()
    engine.settings.forecast_min_samples = 5
    decision = T0 + timedelta(hours=48)

    # Labels must exist before samples can be collected (as run() does).
    from forecast.labels import LabelEngine

    LabelEngine().generate(session, decision_ts=decision)
    session.commit()

    # The collected samples' rows must carry the velocity values at the columns
    # the forecast feature set declares.
    samples = engine._collect_samples(session, decision)
    matrix = engine._matrix(samples)
    columns = list(FORECAST_FEATURE_NAMES)
    kol_idx = columns.index("kol_velocity")
    stars_idx = columns.index("github_star_velocity")
    downloads_idx = columns.index("hf_download_velocity")
    ids = {asset.id: asset.symbol for asset in (flat, drop_a, drop_b, late)}
    drop_rows = [
        index for index, sample in enumerate(samples) if ids[sample.asset_id] in ("DROP", "DROP2")
    ]
    assert drop_rows, "crash assets must produce labeled samples"
    for index in drop_rows:
        assert matrix[index][stars_idx] == 20.0
        assert matrix[index][kol_idx] == 2.0
        assert matrix[index][downloads_idx] == 500.0
    flat_rows = [index for index, sample in enumerate(samples) if ids[sample.asset_id] == "FLAT"]
    assert flat_rows
    for index in flat_rows:
        assert matrix[index][stars_idx] == 0.0  # missing -> honest zero

    ab_result = engine.run_velocity_ab_experiment(session, decision_ts=decision)
    session.commit()
    assert ab_result["status"] == "ok"
    assert ab_result["samples"] == ab_result["train_samples"] + ab_result["test_samples"]
    assert set(ab_result["full"]) == {
        "precision_at_10",
        "calibration_error",
        "median_lead_time_hours",
    }
    assert set(ab_result["velocity_masked"]) == set(ab_result["full"])
    assert set(ab_result["delta"]) == set(ab_result["full"])
    assert ab_result["masked_features"] == [
        "kol_velocity",
        "github_star_velocity",
        "hf_download_velocity",
    ]
    assert session.scalar(select(func.count()).select_from(models.Forecast)) == 0
    ab_metrics = session.scalars(
        select(models.BacktestResult).where(
            models.BacktestResult.run_id == ab_result["run_id"]
        )
    ).all()
    assert any(row.metric_name == "forecast_ab.full.precision_at_10" for row in ab_metrics)
    assert any(
        row.metric_name == "forecast_ab.velocity_masked.calibration_error"
        for row in ab_metrics
    )
    assert any(row.metric_name == "forecast_ab.delta.median_lead_time_hours" for row in ab_metrics)

    result = engine.run(session, decision_ts=decision)
    session.commit()
    assert result["status"] == "ok"
    forecast_rows = session.scalars(select(models.Forecast)).all()
    assert forecast_rows
    contribution = forecast_rows[0].details["feature_contributions"]
    for name in ("kol_velocity", "github_star_velocity", "hf_download_velocity"):
        assert name in contribution
        assert {
            "value",
            "baseline",
            "missing",
            "p_ignition_delta",
            "p_collapse_delta",
        } <= set(contribution[name])
    drift_rows = session.scalars(
        select(models.BacktestResult).where(
            models.BacktestResult.metric_name == "forecast.drift.status"
        )
    ).all()
    assert drift_rows, "drift baseline must persist on the re-run"
    drift_health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "forecast_drift")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert drift_health is not None
    assert drift_health.state in {"ok", "yellow"}


def test_phase_conditioned_hazards(session) -> None:
    """The survival model learns phase-dependent hazards: time-to-collapse is
    conditioned on the lifecycle phase at the label's decision time, the phase
    fit is selected at predict time with a global fallback, and per-phase
    survival curves are persisted as metrics."""
    # FAST crashes 12h in (the ignition bucket decays fast); SLOW crashes at
    # hour 40 (mostly censored from a 24h forward window, so the seeding curve
    # stays long). Both are kept crash-side so the ordering is driven by the
    # phase prior, not by label imbalance.
    fast = _seed_arc(session, symbol="FAST", prices=[1.0] * 12 + [0.2] * 37)
    slow = _seed_arc(session, symbol="SLOW", prices=[1.0] * 40 + [0.2] * 9)
    pump = _seed_arc(session, symbol="PUMP", prices=[1.0] * 30 + [2.0] * 19)
    flat = _seed_arc(session, symbol="FLAT", prices=[1.0] * 49)
    phases = {"FAST": 1.0, "SLOW": 0.0, "PUMP": 2.0, "FLAT": 0.0}

    for asset in (fast, slow, pump, flat):
        for hour in range(0, 25):
            _seed_features(session, asset, hour)
            ts = T0 + timedelta(hours=hour)
            upsert_feature(
                session,
                asset_id=asset.id,
                decision_ts=ts,
                feature_name="lifecycle_phase",
                feature_value=phases[asset.symbol],
                source_count=1,
                freshness_score=1.0,
                missing_flag=False,
            )
    session.commit()

    engine = ForecastEngine()
    engine.settings.forecast_min_samples = 5
    decision = T0 + timedelta(hours=48)
    from forecast.labels import LabelEngine

    LabelEngine().generate(session, decision_ts=decision)
    session.commit()

    samples = engine._collect_samples(session, decision)
    assert samples
    ids = {asset.id: asset.symbol for asset in (fast, slow, pump, flat)}
    # The lifecycle_phase feature must reach the training matrix at the declared
    # column with each asset's phase value.
    phase_idx = list(FORECAST_FEATURE_NAMES).index("lifecycle_phase")
    for sample in samples:
        matrix_row = engine._matrix([sample])[0]
        assert matrix_row[phase_idx] == phases[ids[sample.asset_id]]

    model = engine._train(session, samples, decision)
    assert model is not None
    # FAST collapsed hours after ignition; SLOW collapsed ~a day later from
    # seeding — the ignition survival curve must decay faster than seeding's.
    ignition_fit = model.hazards_by_phase["ignition"]
    seeding_fit = model.hazards_by_phase["seeding"]
    assert ignition_fit.mean_time_to_collapse is not None
    assert seeding_fit.mean_time_to_collapse is not None
    assert ignition_fit.mean_time_to_collapse < seeding_fit.mean_time_to_collapse
    # PUMP samples were all censored (never collapse) -> no learnable curve,
    # so predict must fall back to the global aggregate fit.
    assert model.hazards_by_phase["parabolic"].mean_time_to_collapse is None
    assert model.hazard.mean_time_to_collapse is not None
    assert (
        model.peak_hazards_by_phase["ignition"].mean_time_to_collapse is None
    ), "FAST never pumped -> no time-to-peak curve for the ignition bucket"

    session.commit()
    predicted = engine._predict(session, model, decision)
    session.commit()
    by_symbol = {ids[row.asset_id]: row for row in predicted}
    assert set(by_symbol) == {"FAST", "SLOW", "PUMP", "FLAT"}
    assert by_symbol["FAST"].details["hazard_phase"] == "ignition"
    assert by_symbol["SLOW"].details["hazard_phase"] == "seeding"
    assert by_symbol["PUMP"].details["hazard_phase"] == "parabolic"
    # Same p-level implies different expected hours purely from the phase prior.
    assert DiscreteHazardModel.expected_hours(ignition_fit, 0.9) is not None
    assert DiscreteHazardModel.expected_hours(
        ignition_fit, 0.9
    ) < DiscreteHazardModel.expected_hours(seeding_fit, 0.9)
    fast_expected = by_symbol["FAST"].expected_hours_to_collapse
    slow_expected = by_symbol["SLOW"].expected_hours_to_collapse
    if fast_expected is not None and slow_expected is not None:
        assert fast_expected < slow_expected

    # Per-phase survival curves persist as backtest metrics.
    rows = {
        row.metric_name: row.metric_value
        for row in session.scalars(select(models.BacktestResult)).all()
    }
    assert rows.get("forecast.hazard.ignition.mean_hours_to_collapse") is not None
    assert rows.get("forecast.hazard.seeding.mean_hours_to_collapse") is not None
    assert rows.get("forecast.hazard.phases_fit") == 3.0


def test_forecast_engine_degrades_with_insufficient_data(session) -> None:
    asset = _seed_arc(session, symbol="ONLY", prices=[1.0] * 49)
    _seed_features(session, asset, 0)
    session.commit()
    engine = ForecastEngine()
    engine.settings.forecast_min_samples = 1000
    result = engine.run(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert result["status"] == "insufficient_data"
    assert session.scalar(select(func.count()).select_from(models.Forecast)) == 0


# ── calibration-gap guard ─────────────────────────────────────────────────


def _gap_settings(**overrides: object) -> Settings:
    return Settings(
        forecast_cal_gap_min_samples=1,
        forecast_cal_gap_threshold=0.10,
        forecast_cal_gap_cooldown_hours=24,
        **overrides,
    )


def _latest_gap_row(session) -> models.SystemHealth | None:
    return session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == CALIBRATION_GAP_HEALTH_COMPONENT)
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )


def test_calibration_gap_warns_when_over_threshold(session, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_notify(gap, blended, real, samples, *, threshold, settings=None):
        calls.append(
            {"gap": gap, "blended": blended, "real": real, "threshold": threshold}
        )
        return True

    monkeypatch.setattr("ops.notifier.notify_calibration_bias", fake_notify)
    result = check_calibration_gap(
        session,
        blended_cal=0.2,
        real_cal=0.4,
        real_test_samples=20,
        settings=_gap_settings(),
    )
    session.commit()
    assert result["status"] == "red"
    assert result["pushed"] is True
    assert result["gap"] == pytest.approx(0.2)
    assert len(calls) == 1
    assert calls[0]["threshold"] == 0.10
    row = _latest_gap_row(session)
    assert row is not None and row.state == "red"


def test_calibration_gap_quiet_within_threshold(session, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "ops.notifier.notify_calibration_bias",
        lambda *a, **k: calls.append(1) or True,
    )
    result = check_calibration_gap(
        session,
        blended_cal=0.2,
        real_cal=0.25,
        real_test_samples=20,
        settings=_gap_settings(),
    )
    session.commit()
    assert result["status"] == "ok"
    assert result["gap"] == pytest.approx(0.05)
    assert calls == []
    row = _latest_gap_row(session)
    assert row is not None and row.state == "ok"


def test_calibration_gap_skips_on_few_real_samples(session, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "ops.notifier.notify_calibration_bias",
        lambda *a, **k: calls.append(1) or True,
    )
    result = check_calibration_gap(
        session,
        blended_cal=0.2,
        real_cal=0.9,
        real_test_samples=0,
        settings=Settings(forecast_cal_gap_min_samples=5),
    )
    session.commit()
    assert result["status"] == "skipped"
    assert result["reason"] == "insufficient_real_samples"
    assert calls == []
    row = _latest_gap_row(session)
    assert row is not None and row.state == "ok"


def test_calibration_gap_cooldown_suppresses_repeat(session, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "ops.notifier.notify_calibration_bias",
        lambda *a, **k: calls.append(1) or True,
    )
    settings = _gap_settings()
    # An old red row (older than the cooldown) so the first check is due.
    record_health(
        session,
        component=CALIBRATION_GAP_HEALTH_COMPONENT,
        state="red",
        message="old",
        ts=datetime.now(UTC) - timedelta(hours=48),
    )
    session.commit()
    first = check_calibration_gap(
        session,
        blended_cal=0.2,
        real_cal=0.4,
        real_test_samples=20,
        settings=settings,
    )
    assert first["status"] == "red"
    assert first["pushed"] is True
    assert first["cooldown"] is False
    # The fresh red row just written suppresses the repeat within the window.
    second = check_calibration_gap(
        session,
        blended_cal=0.2,
        real_cal=0.4,
        real_test_samples=20,
        settings=settings,
    )
    session.commit()
    assert second["status"] == "red"
    assert second["pushed"] is False
    assert second["cooldown"] is True
    assert len(calls) == 1


# ── real-only usage gate ──────────────────────────────────────────────────


def test_real_metrics_gate_disabled_by_default() -> None:
    engine = ForecastEngine()
    engine._last_metrics = {"real_test_samples": 0.0, "calibration_error_real": 0.0}
    assert engine._real_metrics_untrustworthy() is False


def test_real_metrics_gate_detects_untrustworthy_readout() -> None:
    engine = ForecastEngine()
    engine.settings.forecast_gate_on_real_metrics = True
    # Too few real observed test samples trips the gate.
    engine._last_metrics = {"real_test_samples": 0.0, "calibration_error_real": 0.0}
    assert engine._real_metrics_untrustworthy() is True
    # Real-only calibration error above the cap trips the gate even with
    # enough samples.
    engine._last_metrics = {
        "real_test_samples": 20.0,
        "calibration_error_real": 0.6,
    }
    assert engine._real_metrics_untrustworthy() is True
    # Plenty of samples and a healthy real-only calibration -> trusted.
    engine._last_metrics = {
        "real_test_samples": 20.0,
        "calibration_error_real": 0.1,
    }
    assert engine._real_metrics_untrustworthy() is False


def test_forecast_run_gates_when_real_metrics_untrustworthy(session, monkeypatch) -> None:
    """When the gate is enabled and the real-only readout is untrustworthy, the
    engine emits no forecasts and degrades to yellow health instead."""
    for symbol, prices in {
        "FLAT": [1.0] * 49,
        "DROP": [1.0] * 30 + [0.2] * 19,
        "DROP2": [1.0] * 30 + [0.2] * 19,
        "LATE": [1.0] * 30 + [2.0] * 19,
    }.items():
        asset = _seed_arc(session, symbol=symbol, prices=prices)
        for hour in range(0, 25):
            _seed_features(session, asset, hour, crash=symbol in ("DROP", "DROP2"))
    session.commit()

    engine = ForecastEngine()
    engine.settings.forecast_min_samples = 5
    engine.settings.forecast_gate_on_real_metrics = True
    monkeypatch.setattr(engine, "_real_metrics_untrustworthy", lambda: True)
    result = engine.run(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert result["status"] == "gated"
    assert {"real_test_samples", "calibration_error", "calibration_error_real",
            "precision_at_10_real"} <= set(result["gate"])
    assert session.scalar(select(func.count()).select_from(models.Forecast)) == 0
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "forecast")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert health is not None and health.state == "yellow"
