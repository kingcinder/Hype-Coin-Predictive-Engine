from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from common.config import Settings
from ops.archive import (
    LocalArchiveStore,
    RawEvidenceCompactor,
    due_partitions,
    query_archive,
    run_archive,
)
from storage import models
from storage.database import acquire_sqlite_writer_lock
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_market_snapshot_once,
    store_raw_evidence,
    upsert_asset,
    upsert_pool_and_pair,
)

DECISION_TS = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _settings(tmp_path, **overrides) -> Settings:
    kwargs: dict[str, object] = {
        "archive_enabled": True,
        "archive_backend": "local",
        "archive_local_dir": str(tmp_path),
        "archive_compact_after_hours": 72.0,
        "archive_retention_days": 30,
        "archive_batch_size": 5_000,
    }
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


def _seed_evidence(
    session,
    *,
    days_ago: float,
    count: int = 1,
    payload: dict | None = None,
    batch: str = "a",
):
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
    rows = []
    for index in range(count):
        row = store_raw_evidence(
            session,
            source=source,
            payload=payload or {"fixture": index, "name": f"ev-{batch}-{index}", "batch": batch},
            observed_at=DECISION_TS - timedelta(days=days_ago, hours=index),
        )
        rows.append(row)
    session.flush()
    return chain, source, rows


def _seed_referenced_evidence(session, source, row) -> None:
    chain = get_or_create_chain(
        session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="USDC111111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=DECISION_TS - timedelta(days=400),
    )
    base = upsert_asset(
        session,
        chain_id=chain.id,
        address="TokenArchive11111111111111111111111111111",
        symbol="ARC",
        name="Archive Token",
        first_seen_at=DECISION_TS - timedelta(days=100),
    )
    _, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address="PairArchive111111111111111111111111111111",
        base_asset_id=base.id,
        quote_asset_id=quote.id,
        created_at_source=DECISION_TS - timedelta(days=100),
    )
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=DECISION_TS - timedelta(days=40),
        observed_at=DECISION_TS - timedelta(days=40),
        price_usd=1.0,
        volume_usd=1000,
        raw_evidence_id=row.id,
    )
    session.flush()


def test_compact_writes_partitioned_parquet_and_manifests(session, tmp_path) -> None:
    _, _, rows = _seed_evidence(session, days_ago=11, count=2)
    _seed_evidence(session, days_ago=77, count=1, batch="b")

    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    result = RawEvidenceCompactor(store=store, settings=settings).compact(session, DECISION_TS)

    assert result["compacted"] == 3
    assert result["partitions"] == 2  # june + april
    manifests = session.scalars(select(models.ArchiveManifest)).all()
    assert len(manifests) == 2
    by_month = {m.partition_month: m for m in manifests}
    assert set(by_month) == {4, 6}
    assert by_month[6].row_count == 2
    assert by_month[4].row_count == 1
    assert all(m.object_key.startswith("evidence/source=dexscreener/") for m in manifests)
    for row in rows:
        session.expire_all()
        assert session.get(models.RawEvidenceItem, row.id).archived_at is not None

    parquet_files = store.list_objects("evidence")
    assert len(parquet_files) == 2
    assert any("year=2026/month=06" in key for key in parquet_files)
    assert any("year=2026/month=04" in key for key in parquet_files)


def test_compact_is_idempotent(session, tmp_path) -> None:
    _seed_evidence(session, days_ago=11, count=2)
    settings = _settings(tmp_path)
    compactor = RawEvidenceCompactor(store=LocalArchiveStore(tmp_path), settings=settings)

    first = compactor.compact(session, DECISION_TS)
    second = compactor.compact(session, DECISION_TS)

    assert first["compacted"] == 2
    assert second["compacted"] == 0
    assert second["partitions"] == 0
    manifest_count = session.scalar(select(func.count()).select_from(models.ArchiveManifest))
    assert manifest_count == 1


def test_second_batch_in_same_partition_merges_not_clobbers(session, tmp_path) -> None:
    """A second batch landing in the same (source, year, month) must append to
    the partition file, never overwrite the earlier rows — otherwise the lake
    silently loses history and the retention report shows fake negative growth."""
    _seed_evidence(session, days_ago=11, count=2)
    settings = _settings(tmp_path)
    compactor = RawEvidenceCompactor(store=LocalArchiveStore(tmp_path), settings=settings)

    first = compactor.compact(session, DECISION_TS)
    assert first["compacted"] == 2

    # More evidence in the same month (days_ago=10 is also June).
    _seed_evidence(session, days_ago=10, count=3, batch="b")
    second = compactor.compact(session, DECISION_TS)
    assert second["compacted"] == 3

    manifest = session.scalar(select(models.ArchiveManifest))
    assert manifest is not None
    assert manifest.row_count == 5, "partition must hold both batches"

    rows = query_archive(
        "SELECT count(*) AS n FROM evidence", store=compactor.store, settings=settings
    )
    assert rows == [{"n": 5}]


def test_prune_removes_only_unreferenced_archived_rows(session, tmp_path) -> None:
    chain, source, unreferenced_rows = _seed_evidence(session, days_ago=77, count=1)
    _, _, referenced_rows = _seed_evidence(session, days_ago=77, count=1, batch="b")
    _seed_referenced_evidence(session, source, referenced_rows[0])
    # recent row: archived but inside the retention window -> must survive
    recent = _seed_evidence(session, days_ago=5, count=1, batch="c")[2][0]

    settings = _settings(tmp_path)
    compactor = RawEvidenceCompactor(store=LocalArchiveStore(tmp_path), settings=settings)
    result = compactor.compact(session, DECISION_TS)

    assert result["pruned"] == 1
    session.expire_all()
    assert session.get(models.RawEvidenceItem, unreferenced_rows[0].id) is None
    assert session.get(models.RawEvidenceItem, referenced_rows[0].id) is not None
    assert session.get(models.RawEvidenceItem, recent.id) is not None
    assert chain.id  # keep reference for linters


def test_query_archive_exposes_lake_to_duckdb(session, tmp_path) -> None:
    _seed_evidence(session, days_ago=11, count=3)
    _seed_evidence(session, days_ago=77, count=2, batch="b")
    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    RawEvidenceCompactor(store=store, settings=settings).compact(session, DECISION_TS)

    rows = query_archive("SELECT count(*) AS n FROM evidence", store=store, settings=settings)
    assert rows == [{"n": 5}]

    by_source = query_archive(
        "SELECT source_type, count(*) AS n FROM evidence GROUP BY source_type",
        store=store,
        settings=settings,
    )
    assert by_source == [{"source_type": "market_data", "n": 5}]


def test_query_archive_empty_lake_returns_empty(session, tmp_path) -> None:
    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    assert query_archive("SELECT count(*) AS n FROM evidence", store=store, settings=settings) == []


def test_due_partitions_reports_only_aged_partitions(session, tmp_path) -> None:
    """The per-partition schedule: only partitions whose unarchived evidence
    has aged past ARCHIVE_COMPACT_AFTER_HOURS are due."""
    _, source, _ = _seed_evidence(session, days_ago=77, count=1)  # April: due
    _seed_evidence(session, days_ago=11, count=2, batch="b")  # June: due
    _seed_evidence(session, days_ago=1, count=1, batch="c")  # June, fresh: NOT due

    settings = _settings(tmp_path)
    due = due_partitions(session, DECISION_TS, settings)

    assert (source.id, 2026, 4) in due
    assert (source.id, 2026, 6) in due
    assert len(due) == 2


def test_compact_partition_filter_compacts_only_due_partitions(session, tmp_path) -> None:
    """Passing the per-partition schedule restricts compaction to exactly the
    due partitions; other aged partitions stay unarchived for a later pass."""
    _, source, april_rows = _seed_evidence(session, days_ago=77, count=1)
    _, _, june_rows = _seed_evidence(session, days_ago=11, count=1, batch="b")

    settings = _settings(tmp_path)
    compactor = RawEvidenceCompactor(store=LocalArchiveStore(tmp_path), settings=settings)
    result = compactor.compact(session, DECISION_TS, partition_filter={(source.id, 2026, 6)})

    assert result["compacted"] == 1
    assert result["partitions"] == 1
    assert result["due_partitions"] == 1
    session.expire_all()
    # June evidence (11 days old, inside the retention window) was compacted.
    assert session.get(models.RawEvidenceItem, june_rows[0].id).archived_at is not None
    # April evidence is due but was not in the filter: left for a later pass.
    april = session.get(models.RawEvidenceItem, april_rows[0].id)
    assert april is not None
    assert april.archived_at is None
    manifests = session.scalars(select(models.ArchiveManifest)).all()
    assert len(manifests) == 1
    assert manifests[0].partition_month == 6


def test_compact_empty_filter_skips_work_but_prunes(session, tmp_path) -> None:
    """A pass with no due partitions does zero compaction work, but pruning
    still runs so expired rows do not accumulate in the hot DB."""
    _, _, rows = _seed_evidence(session, days_ago=11, count=2)  # archived, inside 30d window
    settings = _settings(tmp_path)
    compactor = RawEvidenceCompactor(store=LocalArchiveStore(tmp_path), settings=settings)

    first = compactor.compact(session, DECISION_TS)
    assert first["compacted"] == 2
    assert first["pruned"] == 0  # 11 days old: inside the retention window

    # 25 days later: nothing is due for compaction, but the archived rows have
    # aged past the 30-day retention window -> pruning removes them.
    later = DECISION_TS + timedelta(days=25)
    result = compactor.compact(session, later, partition_filter=set())
    assert result["compacted"] == 0
    assert result["partitions"] == 0
    assert result["due_partitions"] == 0
    assert result["pruned"] == 2
    session.expire_all()
    for row in rows:
        assert session.get(models.RawEvidenceItem, row.id) is None


def test_run_archive_disabled_skips(session, tmp_path) -> None:
    settings = _settings(tmp_path, archive_enabled=False)
    result = run_archive(session, settings=settings)
    assert result == {"skipped": True}
    assert session.scalar(select(func.count()).select_from(models.ArchiveManifest)) == 0


def test_run_archive_records_health(session, tmp_path) -> None:
    _seed_evidence(session, days_ago=11, count=1)
    settings = _settings(tmp_path)
    result = run_archive(session, settings=settings)
    assert result["compacted"] == 1
    health = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "archive")
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    assert health is not None
    assert health.state == "ok"
    assert "compacted" in (health.message or "")


def test_local_single_profile_defaults_to_sqlite_and_local_backend() -> None:
    settings = Settings(_env_file=None, env="local-single")
    assert settings.database_url == "sqlite:///serpent.db"
    assert settings.archive_backend == "local"
    assert settings.archive_backend_is_local


def test_explicit_overrides_win_over_profile() -> None:
    settings = Settings(
        _env_file=None,
        env="local-single",
        database_url="postgresql+psycopg://user:pass@db:5432/serpent",
        archive_backend="s3",
    )
    assert settings.database_url == "postgresql+psycopg://user:pass@db:5432/serpent"
    assert settings.archive_backend == "s3"
    assert not settings.archive_backend_is_local


def test_acquire_sqlite_writer_lock_blocks_a_second_writer(tmp_path) -> None:
    """A second engine opening the same SQLite file hits the single-writer
    guard and fails fast instead of wedging the loop on lock contention."""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/serpent.db",
    )
    fd = acquire_sqlite_writer_lock(settings)
    assert fd is not None
    try:
        with pytest.raises(RuntimeError, match="already holds the SQLite writer lock"):
            acquire_sqlite_writer_lock(settings)
    finally:
        os.close(fd)


def test_acquire_sqlite_writer_lock_noop_for_non_sqlite() -> None:
    """Postgres (docker profile) handles concurrent writers itself, so the guard
    is a no-op and never blocks those engines."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://user:pass@db:5432/serpent",
    )
    assert acquire_sqlite_writer_lock(settings) is None


def test_sqlite_busy_timeout_config_default() -> None:
    """The busy timeout is configurable and defaults well above SQLite's own mask
    spurious intra-process write collisions."""
    settings = Settings(_env_file=None)
    assert settings.sqlite_busy_timeout_ms >= 5000


def test_raw_evidence_payload_sanitizes_datetimes(session) -> None:
    """Crawler payloads may embed datetime objects; the JSON column must not
    roll back the whole scan. Datetimes round-trip as ISO strings."""
    source = get_or_create_source(
        session,
        name="github_public",
        source_type="public_metadata",
        tier="public_metadata",
        base_url="https://api.github.com",
    )
    row = store_raw_evidence(
        session,
        source=source,
        payload={
            "items": [
                {
                    "published": DECISION_TS,
                    "metrics": {"stars": 1, "nested": {"seen": DECISION_TS}},
                    "tags": {DECISION_TS, "b"},
                    "blob": b"bytes",
                }
            ]
        },
        observed_at=DECISION_TS,
        raw_path="narrative:github_public:test",
    )
    session.flush()
    stored = session.get(models.RawEvidenceItem, row.id)
    assert stored is not None
    item = stored.payload["items"][0]
    assert item["published"] == DECISION_TS.isoformat()
    assert item["metrics"]["nested"]["seen"] == DECISION_TS.isoformat()
    assert item["blob"] == "bytes"
    assert sorted(item["tags"]) == [DECISION_TS.isoformat(), "b"]
