from __future__ import annotations

from common.config import Settings
from ingestion import scheduler as scheduler_module


class _FakeScheduler:
    instances: list[_FakeScheduler] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.jobs: list[tuple[object, str, dict[str, object]]] = []
        self.started = False
        self.instances.append(self)

    def add_job(self, func, trigger: str, **kwargs) -> None:
        self.jobs.append((func, trigger, kwargs))

    def start(self) -> None:
        self.started = True


def test_scheduler_registers_forecast_training_job(monkeypatch) -> None:
    _FakeScheduler.instances.clear()
    settings = Settings(
        forecast_enabled=True,
        forecast_train_frequency_hours=12,
        retention_autopilot_enabled=False,
    )
    monkeypatch.setattr(scheduler_module, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", _FakeScheduler)
    monkeypatch.setattr(scheduler_module, "ensure_background_probe", lambda: None)
    monkeypatch.setattr(scheduler_module, "run_once", lambda: {})

    scheduler_module.main()

    scheduler = _FakeScheduler.instances[0]
    forecast_jobs = [job for job in scheduler.jobs if job[2].get("id") == "forecast_training"]
    assert len(forecast_jobs) == 1
    _, trigger, options = forecast_jobs[0]
    assert trigger == "interval"
    assert options["hours"] == 12
    assert options["coalesce"] is True
    assert options["max_instances"] == 1
    assert scheduler.started is True
