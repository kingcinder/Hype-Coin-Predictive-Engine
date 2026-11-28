"""Ticket 1 step 4a (M2): inference decoupled from the training cadence.

Before this change Forecast rows were written only inside the 24h-cadence
training run, so _forecast_probability served a frozen probability between
retrains. The scan now runs an inference-only pass from the persisted model
whenever training is not due.
"""

from __future__ import annotations

from datetime import UTC, datetime

import forecast.engine as forecast_engine_module
from forecast.engine import run_forecast_cycle, run_forecast_inference

DECISION = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def test_inference_without_persisted_model_reports_no_model(session, monkeypatch) -> None:
    monkeypatch.setattr("forecast.persistence.load_model", lambda version=None: None)
    result = run_forecast_inference(session, decision_ts=DECISION)
    assert result["status"] == "no_model"
    assert result.get("forecasts", 0) == 0


def test_inference_with_model_writes_forecasts(session, monkeypatch) -> None:
    monkeypatch.setattr("forecast.persistence.load_model", lambda version=None: object())
    calls: list[datetime] = []

    def fake_predict(self, sess, model, decision_ts):  # noqa: ANN001, ANN202
        calls.append(decision_ts)
        return [object(), object(), object()]

    monkeypatch.setattr(forecast_engine_module.ForecastEngine, "_predict", fake_predict)
    result = run_forecast_inference(session, decision_ts=DECISION)
    assert result["status"] == "ok"
    assert result["forecasts"] == 3
    assert calls == [DECISION]


def test_cycle_runs_inference_when_training_not_due(session, monkeypatch) -> None:
    monkeypatch.setattr(forecast_engine_module, "forecast_due", lambda s, settings=None: False)
    monkeypatch.setattr("forecast.persistence.load_model", lambda version=None: object())
    monkeypatch.setattr(
        forecast_engine_module.ForecastEngine,
        "_predict",
        lambda self, sess, model, decision_ts: [object()],
    )
    result = run_forecast_cycle(session, decision_ts=DECISION)
    assert result["status"] == "ok"
    assert result["forecasts"] == 1


def test_cycle_trains_when_due(session, monkeypatch) -> None:
    monkeypatch.setattr(forecast_engine_module, "forecast_due", lambda s, settings=None: True)
    monkeypatch.setattr(
        forecast_engine_module.ForecastEngine,
        "run",
        lambda self, sess, decision_ts=None: {"status": "trained", "forecasts": 9},
    )
    result = run_forecast_cycle(session, decision_ts=DECISION)
    assert result["status"] == "trained"
    assert result["forecasts"] == 9
