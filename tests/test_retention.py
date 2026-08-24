from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from common.config import Settings
from ops.archive import LocalArchiveStore, RawEvidenceCompactor
from ops.retention import lake_report, retention_due, run_retention
from storage import models
from storage.repository import get_or_create_chain, get_or_create_source, store_raw_evidence

DECISION_TS = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _settings(tmp_path, **overrides) -> Settings:
    kwargs: dict[str, object] = {
        "archive_enabled": True,
        "archive_backend": "local",
        "archive_local_dir": str(tmp_path),
        "archive_compact_after_hours": 72.0,
        "archive_retention_days": 30,
        "archive_batch_size": 5_000,
        "retention_autopilot_enabled": True,
        "retention_cadence_hours": 24.0,
    }
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


def _seed_evidence(session, *, count: int = 2, days_ago: float = 11.0, batch: str = "a") -> None:
    chain = get_or_create_chain(
        session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
    )
    source = get_or_create_source(
        session,
        name="dexscreener",
        source_type="market_data",
        tier="venue",
        base_url="https://api.dexscreener.com",
    )
    for index in range(count):
        store_raw_evidence(
            session,
            source=source,
            payload={"fixture": index, "batch": batch},
            observed_at=DECISION_TS - timedelta(days=days_ago, hours=index),
        )
    session.flush()
    assert chain.id  # keep reference for linters


def _compact(session, tmp_path, settings: Settings) -> None:
    RawEvidenceCompactor(
        store=LocalArchiveStore(tmp_path), settings=settings
    ).compact(session, DECISION_TS)
    session.flush()


def test_retention_first_run_records_totals_and_health(session, tmp_path) -> None:
    _seed_evidence(session, count=3)
    settings = _settings(tmp_path)
    _compact(session, tmp_path, settings)

    result = run_retention(session, decision_ts=DECISION_TS, settings=settings)
    session.flush()

    assert result["status"] == "ok"
    assert result["partitions"] == 1
    assert result["archived_rows"] == 3
    assert result["byte_size"] > 0
    assert result["growth_bytes"] == result["byte_size"]  # first pass: whole lake is new
    assert result["growth_pct"] is None

    run = session.scalar(select(models.RetentionRun))
    assert run is not None
    assert run.partitions == 1
    assert run.archived_rows == 3
    assert run.growth_pct is None
    assert run.duration_sec is not None

    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "lake")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert health.state == "ok"
    assert "growth_bytes" in (health.message or "")
    assert "partitions=1" in (health.message or "")


def test_retention_second_run_reports_growth(session, tmp_path) -> None:
    _seed_evidence(session, count=2)
    settings = _settings(tmp_path)
    _compact(session, tmp_path, settings)
    first = run_retention(session, decision_ts=DECISION_TS, settings=settings)
    session.flush()

    # More evidence lands in the same partition; compaction grows the lake.
    _seed_evidence(session, count=2, days_ago=10.0, batch="b")
    _compact(session, tmp_path, settings)
    second = run_retention(
        session, decision_ts=DECISION_TS + timedelta(days=1), settings=settings
    )
    session.flush()

    assert second["status"] == "ok"
    assert second["growth_bytes"] > 0
    assert second["growth_pct"] is not None
    assert second["growth_bytes"] == second["byte_size"] - first["byte_size"]
    runs = session.scalars(
        select(models.RetentionRun).order_by(models.RetentionRun.ts)
    ).all()
    assert len(runs) == 2
    assert runs[1].growth_bytes == second["growth_bytes"]
    assert runs[1].growth_pct == second["growth_pct"]


def test_retention_disabled_skips(session, tmp_path) -> None:
    settings = _settings(tmp_path, retention_autopilot_enabled=False)
    result = run_retention(session, decision_ts=DECISION_TS, settings=settings)
    assert result == {"skipped": True}
    assert session.scalar(select(func.count()).select_from(models.RetentionRun)) == 0
    assert (
        session.scalar(
            select(func.count())
            .select_from(models.SystemHealth)
            .where(models.SystemHealth.component == "lake")
        )
        == 0
    )


def test_retention_failure_records_red_health(session, tmp_path, monkeypatch) -> None:
    from ops import retention

    def _boom(*args, **kwargs):
        return {"error": "lake exploded"}

    monkeypatch.setattr(retention, "run_archive", _boom)
    settings = _settings(tmp_path)
    result = run_retention(session, decision_ts=DECISION_TS, settings=settings)
    session.flush()

    assert result == {"error": "lake exploded"}
    assert session.scalar(select(func.count()).select_from(models.RetentionRun)) == 0
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "lake")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert health.state == "red"
    assert health.error_count == 1


def test_retention_due_uses_cadence(session, tmp_path) -> None:
    settings = _settings(tmp_path)
    # No recorded pass -> due (first run).
    assert retention_due(session, now=DECISION_TS, settings=settings) is True

    _seed_evidence(session, count=1)
    _compact(session, tmp_path, settings)
    run_retention(session, decision_ts=DECISION_TS, settings=settings)
    session.flush()

    # Fresh pass -> not due until the cadence elapses.
    assert (
        retention_due(session, now=DECISION_TS + timedelta(hours=23), settings=settings)
        is False
    )
    assert (
        retention_due(session, now=DECISION_TS + timedelta(hours=25), settings=settings)
        is True
    )

    # Disabled autopilot is never due.
    disabled = _settings(tmp_path, retention_autopilot_enabled=False)
    assert retention_due(session, now=DECISION_TS + timedelta(days=3), settings=disabled) is False


def test_lake_report_totals_from_manifests(session, tmp_path) -> None:
    _seed_evidence(session, count=2, days_ago=11.0)
    _seed_evidence(session, count=1, days_ago=77.0, batch="b")
    settings = _settings(tmp_path)
    _compact(session, tmp_path, settings)

    report = lake_report(session)
    assert report["partitions"] == 2
    assert report["archived_rows"] == 3
    assert report["byte_size"] > 0
