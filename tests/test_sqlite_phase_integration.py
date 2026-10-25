from __future__ import annotations

import threading

from common.config import Settings
from data_lake.manager import run_data_lake_pass
from engine.run import run_engine_phases
from ingestion import service as service_module
from ingestion.service import IngestionService
from ops.parity import run_parity
from ops.retention import run_retention
from scripts.seed_fixtures import seed_fixture_data


class _State:
    def mark_forecasting(self) -> None: ...

    def mark_retention(self) -> None: ...

    def mark_error(self, _message: str) -> None: ...


class _RpcPool:
    def probe_down_endpoints(self, _probe) -> int:
        return 0

    def dispatch_alerts(self, _notify, *, cooldown_seconds: float) -> int:
        return 0


def test_seeded_sqlite_scan_data_lake_retention_and_parity(session, tmp_path, monkeypatch) -> None:
    """Exercise local scan and maintenance phases without live sources."""
    settings = Settings(
        _env_file=None,
        env="local-single",
        archive_enabled=True,
        archive_backend="local",
        archive_local_dir=str(tmp_path),
        archive_compact_after_hours=0.0,
        retention_autopilot_enabled=True,
        data_lake_enabled=True,
        parity_enabled=True,
        forecast_timeout_seconds=5.0,
        retention_timeout_seconds=5.0,
        parity_timeout_seconds=5.0,
        data_lake_timeout_seconds=5.0,
        skip_alert_cycles=100,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    seed_fixture_data(session=session)
    service = IngestionService()
    for name in (
        "_ingest_dexscreener_profiles",
        "_ingest_dexscreener_boosts",
        "_ingest_geckoterminal_new_pools",
        "_ingest_birdeye_solana",
        "_ingest_solana_holder_snapshots",
        "_ingest_solana_safety_snapshots",
        "_ingest_evm_holder_snapshots",
    ):
        monkeypatch.setattr(service, name, lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(service, "_run_contract_analysis", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service_module, "run_mempool", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service_module, "run_narrative", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service_module, "extract_catalysts", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(service_module, "alert_upcoming_catalysts", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(service_module, "run_lifecycle", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(service_module, "run_notifier", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service_module, "run_forecast_if_due", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(service_module, "get_rpc_pool", lambda _chain: _RpcPool())
    monkeypatch.setattr(service_module, "persist_pool_snapshots", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(service_module, "record_pool_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service_module, "probe_rpc_endpoint", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(service_module.LiquidityRemovalWatcher, "scan", lambda *_a, **_k: {})
    monkeypatch.setattr(service_module.PrelaunchQueue, "scan", lambda *_a, **_k: [])
    monkeypatch.setattr(service_module.IgnitionRadar, "scan", lambda *_a, **_k: {})
    monkeypatch.setattr(service_module.FingerprintEngine, "learn", lambda *_a, **_k: {})
    monkeypatch.setattr(service_module.FingerprintEngine, "assess", lambda *_a, **_k: [])

    scan = service.run_once(session)
    assert scan["errors"] == []
    for key in ("data_quality", "contract_analysis"):
        assert "error" not in scan[key]

    def _commit_phase(result: dict[str, object]) -> dict[str, object]:
        session.commit()
        return result

    phase_error, _ = run_engine_phases(
        settings=settings,
        iteration=1,
        stop=threading.Event(),
        last_nc_run_monotonic=0.0,
        system_state=_State(),
        alarm_session=session,
        forecast_fn=lambda: {"status": "skipped"},
        retention_fn=lambda: _commit_phase(run_retention(session, settings=settings)),
        parity_fn=lambda: _commit_phase(run_parity(session, settings=settings)),
        nightcrawler_fn=lambda: {},
        data_lake_fn=lambda: _commit_phase(run_data_lake_pass(session)),
        score_drift_fn=lambda: {"status": "skipped"},
    )
    assert phase_error is False
