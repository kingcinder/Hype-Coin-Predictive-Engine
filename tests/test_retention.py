from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from common.config import Settings
from ops.archive import LocalArchiveStore, RawEvidenceCompactor
from ops.retention import (
    check_lake_budget,
    check_lake_freshness,
    lake_report,
    project_lake_growth,
    retention_due,
    run_retention,
)
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
    # A single pass has no growth trend: the budget check stays quiet.
    assert result["lake_budget"] == {"alert": False, "days_to_full": None}

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


def test_two_real_retention_passes_page_once_at_capacity(session, tmp_path, monkeypatch) -> None:
    """The end-to-end retention path pages once when a growing lake reaches a
    tiny configured cap, and records a red lake_budget health row."""
    settings = _settings(
        tmp_path,
        archive_lake_max_bytes=1,
        retention_budget_alert_days=14.0,
        retention_budget_alert_cooldown_hours=24.0,
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "ops.retention.notify_lake_budget",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    _seed_evidence(session, count=2, days_ago=11.0, batch="first")
    first = run_retention(session, decision_ts=DECISION_TS, settings=settings)
    session.flush()
    assert first["status"] == "ok"

    _seed_evidence(session, count=2, days_ago=10.0, batch="second")
    second = run_retention(
        session, decision_ts=DECISION_TS + timedelta(days=1), settings=settings
    )
    session.flush()

    assert second["status"] == "ok"
    assert second["lake_budget"]["state"] == "red"
    assert second["lake_budget"]["days_to_full"] == 0.0
    assert second["lake_budget"]["pushed"] is True
    assert len(calls) == 1
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "lake_budget")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert health.state == "red"


def test_retention_spreads_due_partitions_across_passes(session, tmp_path) -> None:
    settings = _settings(tmp_path, retention_max_partitions_per_pass=1)
    _seed_evidence(session, count=1, days_ago=11.0, batch="recent")
    _seed_evidence(session, count=1, days_ago=77.0, batch="old")

    first = run_retention(session, decision_ts=DECISION_TS, settings=settings)
    session.flush()
    assert first["status"] == "ok"
    assert first["due_partitions"] == 1
    assert first["due_partitions_remaining"] == 1
    assert first["compacted"] == 1

    second = run_retention(
        session, decision_ts=DECISION_TS + timedelta(days=1), settings=settings
    )
    session.flush()
    assert second["status"] == "ok"
    assert second["due_partitions"] == 1
    assert second["due_partitions_remaining"] == 0
    assert second["compacted"] == 1


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


def test_retention_compaction_invalidates_lake_cache(
    session, tmp_path, monkeypatch
) -> None:
    """A retention pass that compacts new evidence clears the
    LakeFeatureFactory (asset, hour) cache so long-lived processes don't serve
    stale reconstructions; a pass with nothing due keeps the cache warm."""
    from ops import retention

    _seed_evidence(session, count=3, days_ago=11.0)
    settings = _settings(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(
        retention.LakeFeatureFactory,
        "clear_cache",
        classmethod(lambda cls: calls.__setitem__("n", calls["n"] + 1)),
    )

    # First pass: the aged evidence is due, so run_archive compacts it into the
    # lake -> the cached reconstructions are stale and must be invalidated.
    first = run_retention(session, decision_ts=DECISION_TS, settings=settings)
    session.flush()
    assert first["status"] == "ok"
    assert first["compacted"] > 0
    assert calls["n"] == 1

    # Second pass immediately after: nothing new is due, zero compaction -> the
    # cache stays warm (no invalidation).
    second = run_retention(
        session, decision_ts=DECISION_TS + timedelta(hours=1), settings=settings
    )
    session.flush()
    assert second["compacted"] == 0
    assert calls["n"] == 1


def test_check_lake_budget_fires_when_horizon_within_alert_days(
    session, tmp_path, monkeypatch
) -> None:
    """Projected disk-full horizon inside RETENTION_BUDGET_ALERT_DAYS records
    yellow lake_budget health and pushes an ntfy warning."""
    settings = _settings(
        tmp_path,
        archive_lake_max_bytes=30 * 1024**3,
        retention_budget_alert_days=14.0,
        retention_budget_alert_cooldown_hours=24.0,
    )
    # 2 GiB/day growth over 5 passes: last pass at 18 GiB, 12 GiB left -> 6 days.
    history = [
        _run(DECISION_TS + timedelta(days=day), (10 + 2 * day) * 1024**3)
        for day in range(5)
    ]
    calls: list[dict[str, object]] = []

    def _fake_push(**kwargs) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr("ops.retention.notify_lake_budget", _fake_push)
    out = check_lake_budget(
        session, history=history, now=DECISION_TS, settings=settings
    )
    session.flush()

    assert out["alert"] is True
    assert out["state"] == "yellow"
    assert out["days_to_full"] == 6.0
    assert out["pushed"] is True
    assert len(calls) == 1
    assert calls[0]["days_to_full"] == 6.0
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "lake_budget")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert health.state == "yellow"
    assert "lake budget" in (health.message or "")


def test_check_lake_budget_respects_cooldown(session, tmp_path, monkeypatch) -> None:
    """Repeated passes within the cooldown refresh the health row but do not
    spam the same ntfy warning."""
    settings = _settings(
        tmp_path,
        archive_lake_max_bytes=30 * 1024**3,
        retention_budget_alert_days=14.0,
        retention_budget_alert_cooldown_hours=24.0,
    )
    history = [
        _run(DECISION_TS + timedelta(days=day), (10 + 2 * day) * 1024**3)
        for day in range(5)
    ]
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "ops.retention.notify_lake_budget", lambda **kw: calls.append(kw) or True
    )

    first = check_lake_budget(
        session, history=history, now=DECISION_TS, settings=settings
    )
    # A pass 1h later is still inside the 24h cooldown: health row refreshes,
    # but the ntfy warning is not re-pushed.
    second = check_lake_budget(
        session,
        history=history,
        now=DECISION_TS + timedelta(hours=1),
        settings=settings,
    )
    session.flush()

    assert first["pushed"] is True
    assert second["pushed"] is False  # same cooldown window
    assert len(calls) == 1
    health_count = session.scalar(
        select(func.count())
        .select_from(models.SystemHealth)
        .where(models.SystemHealth.component == "lake_budget")
    )
    assert health_count == 2  # health refreshed every pass, push only once


def test_check_lake_budget_quiet_within_budget(session, tmp_path, monkeypatch) -> None:
    """A horizon beyond the alert window records nothing and pushes nothing;
    a lake with no usable trend is quiet too."""
    settings = _settings(
        tmp_path,
        archive_lake_max_bytes=60 * 1024**3,
        retention_budget_alert_days=14.0,
        retention_budget_alert_cooldown_hours=24.0,
    )
    # 1 GiB/day over 10 passes: last at 19 GiB, 41 left -> 41 days > 14.
    history = [
        _run(DECISION_TS + timedelta(days=day), (10 + day) * 1024**3)
        for day in range(10)
    ]
    monkeypatch.setattr("ops.retention.notify_lake_budget", lambda **kw: True)

    out = check_lake_budget(
        session, history=history, now=DECISION_TS, settings=settings
    )
    assert out == {"alert": False, "days_to_full": 41.0}
    assert (
        session.scalar(
            select(func.count())
            .select_from(models.SystemHealth)
            .where(models.SystemHealth.component == "lake_budget")
        )
        == 0
    )

    # No usable trend (single pass): quiet.
    out = check_lake_budget(
        session, history=[_run(DECISION_TS, 1_000)], now=DECISION_TS, settings=settings
    )
    assert out == {"alert": False, "days_to_full": None}


def test_check_lake_freshness_records_yellow_when_stale(session, tmp_path) -> None:
    """A lake whose last pass is older than the cadence is surfaced as yellow
    Feed Health before the scan runs (same gate as --check-due)."""
    settings = _settings(tmp_path)
    session.add(
        models.RetentionRun(
            ts=DECISION_TS,
            partitions=1,
            archived_rows=1,
            byte_size=1024,
            compacted=1,
            pruned=0,
            growth_bytes=0,
            growth_pct=None,
            duration_sec=1.0,
        )
    )
    session.flush()

    out = check_lake_freshness(
        session, now=DECISION_TS + timedelta(hours=25), settings=settings
    )
    session.flush()

    assert out["fresh"] is False
    assert out["recorded"] is True
    assert out["stale_hours"] == 25.0
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "lake")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert health.state == "yellow"
    assert "lake stale" in (health.message or "")


def test_check_lake_freshness_escalates_after_stale_cycles(
    session, tmp_path, monkeypatch
) -> None:
    settings = _settings(
        tmp_path,
        retention_stale_warning_after_cycles=3,
        retention_stale_warning_cooldown_hours=24.0,
    )
    session.add(_run(DECISION_TS, 1024))
    session.flush()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "ops.retention.notify_lake_stale",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    out = check_lake_freshness(
        session,
        now=DECISION_TS + timedelta(hours=73),
        settings=settings,
    )
    session.flush()
    assert out["warning_pushed"] is True
    assert out["stale_hours"] == 73.0
    assert len(calls) == 1
    assert calls[0]["stale_cycles"] == 3
    warning = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "lake_stale_warning")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert warning is not None
    assert warning.state == "yellow"


def test_check_lake_freshness_fresh_records_nothing(session, tmp_path) -> None:
    """A lake within the cadence window stays quiet: the last pass already
    wrote the ok row, so the freshness check adds no noise."""
    settings = _settings(tmp_path)
    session.add(
        models.RetentionRun(
            ts=DECISION_TS,
            partitions=1,
            archived_rows=1,
            byte_size=1024,
            compacted=1,
            pruned=0,
            growth_bytes=0,
            growth_pct=None,
            duration_sec=1.0,
        )
    )
    session.flush()

    out = check_lake_freshness(
        session, now=DECISION_TS + timedelta(hours=23), settings=settings
    )
    session.flush()

    assert out["fresh"] is True
    assert out["recorded"] is False
    assert (
        session.scalar(
            select(func.count())
            .select_from(models.SystemHealth)
            .where(models.SystemHealth.component == "lake")
        )
        == 0
    )


def test_check_lake_freshness_no_pass_or_disabled_records_nothing(
    session, tmp_path
) -> None:
    """A brand-new deployment (no pass yet) and a disabled autopilot are
    never flagged stale."""
    settings = _settings(tmp_path)
    out = check_lake_freshness(session, now=DECISION_TS, settings=settings)
    assert out == {"fresh": True, "recorded": False}

    disabled = _settings(tmp_path, retention_autopilot_enabled=False)
    session.add(
        models.RetentionRun(
            ts=DECISION_TS - timedelta(days=3),
            partitions=1,
            archived_rows=1,
            byte_size=1024,
            compacted=1,
            pruned=0,
            growth_bytes=0,
            growth_pct=None,
            duration_sec=1.0,
        )
    )
    session.flush()
    out = check_lake_freshness(
        session, now=DECISION_TS, settings=disabled
    )
    assert out == {"fresh": True, "recorded": False}
    assert (
        session.scalar(
            select(func.count())
            .select_from(models.SystemHealth)
            .where(models.SystemHealth.component == "lake")
        )
        == 0
    )


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


def _run(ts: datetime, byte_size: int) -> models.RetentionRun:
    return models.RetentionRun(
        ts=ts,
        partitions=1,
        archived_rows=1,
        byte_size=byte_size,
        compacted=1,
        pruned=0,
        growth_bytes=0,
        growth_pct=None,
        duration_sec=1.0,
    )


def test_project_lake_growth_single_or_flat_run_has_no_horizon() -> None:
    # A single pass has no trend to fit.
    out = project_lake_growth([_run(DECISION_TS, 1_000)], max_bytes=100_000)
    assert out["growth_rate_bytes_per_hour"] == 0.0
    assert out["projected_full_at"] is None
    assert out["days_to_full"] is None
    assert out["pct_full"] == 1.0
    assert out["sample_runs"] == 1

    # Flat trend across passes: no disk-full horizon, current fill reported.
    runs = [_run(DECISION_TS + timedelta(days=i), 5_000) for i in range(4)]
    out = project_lake_growth(runs, max_bytes=100_000)
    assert out["growth_rate_bytes_per_hour"] == 0.0
    assert out["projected_full_at"] is None
    assert out["days_to_full"] is None
    assert out["pct_full"] == 5.0

    # Empty history reports an empty, full-free projection.
    out = project_lake_growth([], max_bytes=100_000)
    assert out["pct_full"] == 0.0
    assert out["projected_full_at"] is None


def test_project_lake_growth_extrapolates_disk_full_horizon() -> None:
    # Exactly 1 GiB/day growth: 10 passes from 10 GiB to 19 GiB.
    runs = [
        _run(DECISION_TS + timedelta(days=day), 10 * 1024**3 + day * 1024**3)
        for day in range(10)
    ]
    out = project_lake_growth(runs, max_bytes=60 * 1024**3)
    assert out["growth_rate_bytes_per_hour"] == round(1024**3 / 24.0, 2)
    # Last pass is 19 GiB; 41 GiB remain at 1 GiB/day -> 41 days.
    assert out["days_to_full"] == 41.0
    assert out["pct_full"] == 31.67
    assert out["projected_full_at"] is not None
    assert out["sample_runs"] == 10


def test_project_lake_growth_already_full_immediate_horizon() -> None:
    runs = [
        _run(DECISION_TS, 1_000),
        _run(DECISION_TS + timedelta(days=1), 2_000),
    ]
    out = project_lake_growth(runs, max_bytes=1_500)
    assert out["days_to_full"] == 0.0
    assert out["projected_full_at"] is not None
    assert out["pct_full"] == 100.0


def test_project_lake_growth_far_horizon_has_no_practical_date() -> None:
    # 1 KB/day against a 100 GiB cap: the linear extrapolation lands centuries
    # out and must not overflow datetime arithmetic — report no horizon.
    runs = [
        _run(DECISION_TS, 1_000),
        _run(DECISION_TS + timedelta(days=1), 2_000),
    ]
    out = project_lake_growth(runs, max_bytes=100 * 1024**3)
    assert out["growth_rate_bytes_per_hour"] > 0
    assert out["projected_full_at"] is None
    assert out["days_to_full"] is None
    assert out["sample_runs"] == 2
