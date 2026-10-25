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


def _main_with_fake_scheduler(monkeypatch, **settings_kwargs):
    _FakeScheduler.instances.clear()
    settings = Settings(
        forecast_enabled=True,
        backtest_autopilot_enabled=True,
        retention_autopilot_enabled=True,
        archive_enabled=True,
        parity_enabled=True,
        **settings_kwargs,
    )
    monkeypatch.setattr(scheduler_module, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", _FakeScheduler)
    monkeypatch.setattr(scheduler_module, "ensure_background_probe", lambda: None)
    scheduler_module.main()
    return _FakeScheduler.instances[0]


def test_scheduler_scan_job_has_coalesce_and_max_instances(monkeypatch) -> None:
    """M7: the scan job previously lacked coalesce/max_instances entirely."""
    scheduler = _main_with_fake_scheduler(monkeypatch)
    scan_jobs = [job for job in scheduler.jobs if job[2].get("id") == "ingestion_scan"]
    assert len(scan_jobs) == 1
    _, _, options = scan_jobs[0]
    assert options["coalesce"] is True
    assert options["max_instances"] == 1


def test_scheduler_retention_job_has_coalesce_and_max_instances(monkeypatch) -> None:
    scheduler = _main_with_fake_scheduler(monkeypatch)
    jobs = [job for job in scheduler.jobs if job[2].get("id") == "retention_autopilot"]
    assert len(jobs) == 1
    _, _, options = jobs[0]
    assert options["coalesce"] is True
    assert options["max_instances"] == 1


def test_scheduler_jobs_skip_when_another_job_running(monkeypatch) -> None:
    """M7: cross-job mutual exclusion — an overlapping run skips, never collides."""
    calls: list[str] = []
    monkeypatch.setattr(scheduler_module, "run_once", lambda: calls.append("scan"))
    scheduler = _main_with_fake_scheduler(monkeypatch)
    scan_jobs = [job for job in scheduler.jobs if job[2].get("id") == "ingestion_scan"]
    scan_fn = scan_jobs[0][0]

    # Another job holds the lock: the scan skips instead of running.
    with scheduler_module._JOB_LOCK:  # noqa: SLF001 - white-box check
        assert scan_fn() is None
    assert calls == []

    # Lock free: the scan runs normally and releases the lock afterwards.
    assert scan_fn() is None
    assert calls == ["scan"]
    assert not scheduler_module._JOB_LOCK.locked()  # noqa: SLF001


def test_exclusive_wrapper_releases_lock_on_error() -> None:
    """A failing job must not wedge the lock for every subsequent job."""

    def boom():
        raise RuntimeError("job exploded")

    wrapped = scheduler_module._exclusive("boom", boom)
    with scheduler_module._JOB_LOCK:  # noqa: SLF001
        assert wrapped() is None  # skipped while busy
    try:
        wrapped()
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected the job error to propagate")
    assert not scheduler_module._JOB_LOCK.locked()  # noqa: SLF001
