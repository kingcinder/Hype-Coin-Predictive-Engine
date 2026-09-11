"""Regression tests for the archive write-path hardening (REVIEW.md C2, H1, H2, M1, M3).

Covers, against the local backend unless noted:
- C2: a corrupt existing partition fails closed instead of being amputated by
  a merge with only the new batch; the failed rows stay marked unarchived.
- H1: partition writes are atomic renames — no temp files survive and the
  previous object is never left half-written.
- H2: two racing compactions into the same partition converge to the full,
  deduplicated row set (interprocess lock + ETag conditional writes).
- M1: a "PUT succeeded, DB commit failed" retry does not duplicate evidence
  rows in the partition.
- M3: the manifest records the SHA-256 of the exact stored bytes, and the
  compactor verifies the stored size after every write.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from common.config import Settings
from ops.archive import (
    ArchiveWriteError,
    LocalArchiveStore,
    PartitionUnreadableError,
    RawEvidenceCompactor,
)
from storage import models
from storage.repository import (
    get_or_create_source,
    store_raw_evidence,
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


def _seed_evidence(session, *, days_ago: float, count: int = 1, batch: str = "a"):
    source = get_or_create_source(
        session,
        name="dexscreener",
        source_type="market_data",
        tier="venue",
        base_url="https://api.dexscreener.com",
    )
    rows = []
    for index in range(count):
        rows.append(
            store_raw_evidence(
                session,
                source=source,
                payload={"fixture": index, "name": f"ev-{batch}-{index}", "batch": batch},
                observed_at=DECISION_TS - timedelta(days=days_ago, hours=index),
            )
        )
    session.flush()
    return rows


def _partition_key(store: LocalArchiveStore) -> str:
    keys = store.list_objects("evidence")
    assert len(keys) == 1, f"expected a single partition, got {keys}"
    return keys[0]


def test_corrupt_partition_fails_closed_and_keeps_original_rows(session, tmp_path):
    """C2: a corrupt existing partition must raise, not be overwritten."""
    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    _seed_evidence(session, days_ago=11.0, count=2, batch="a")
    compactor = RawEvidenceCompactor(store=store, settings=settings)
    first = compactor.compact(session, DECISION_TS)
    assert first["compacted"] == 2
    session.commit()

    key = _partition_key(store)
    (tmp_path / key).write_bytes(b"this is not parquet")

    _seed_evidence(session, days_ago=12.0, count=1, batch="b")
    session.commit()
    with pytest.raises(PartitionUnreadableError):
        compactor.compact(session, DECISION_TS)
    session.rollback()

    # The corrupt bytes are still there (nothing rewrote them), and the new
    # batch was NOT marked archived.
    assert (tmp_path / key).read_bytes() == b"this is not parquet"
    unarchived = session.scalars(
        select(models.RawEvidenceItem).where(models.RawEvidenceItem.archived_at.is_(None))
    ).all()
    assert len(unarchived) == 1
    assert unarchived[0].payload["batch"] == "b"


def test_atomic_write_leaves_no_temp_files_and_preserves_prior_object(session, tmp_path):
    """H1: put_object lands via atomic rename; no temp file survives."""
    store = LocalArchiveStore(tmp_path)
    key = "evidence/source=x/year=2026/month=06/data.parquet"
    first = b"first-bytes"
    store.put_object(key, first)
    store.put_object(key, b"second-bytes-longer")
    leftovers = [p for p in tmp_path.rglob("*.tmp")]
    assert leftovers == []
    assert (tmp_path / key).read_bytes() == b"second-bytes-longer"


def test_concurrent_compactions_do_not_lose_batches(session, tmp_path):
    """H2: two racing compactions converge to the full, deduplicated set."""
    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    _seed_evidence(session, days_ago=11.0, count=2, batch="a")
    _seed_evidence(session, days_ago=12.0, count=2, batch="b")
    session.flush()

    bind = session.get_bind()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            maker = sessionmaker(bind=bind)
            with maker() as thread_session:
                RawEvidenceCompactor(store=store, settings=settings).compact(
                    thread_session, DECISION_TS
                )
                thread_session.commit()
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    frame = pl.read_parquet(tmp_path / _partition_key(store))
    payloads = frame["payload_json"].to_list()
    assert frame.height == 4
    assert frame["evidence_id"].n_unique() == 4
    for name in ("ev-a-0", "ev-a-1", "ev-b-0", "ev-b-1"):
        assert any(name in payload for payload in payloads), name


def test_failed_commit_retry_does_not_duplicate_rows(session, tmp_path):
    """M1: PUT-then-failed-commit retry must dedupe on evidence_id."""
    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    compactor = RawEvidenceCompactor(store=store, settings=settings)
    _seed_evidence(session, days_ago=11.0, count=3, batch="a")
    compactor.compact(session, DECISION_TS)
    session.commit()

    # Simulate the M1 scenario: object PUT succeeded, DB commit was lost.
    session.execute(
        models.RawEvidenceItem.__table__.update().values(archived_at=None)
    )
    session.commit()

    compactor.compact(session, DECISION_TS)
    session.commit()

    frame = pl.read_parquet(tmp_path / _partition_key(store))
    assert frame.height == 3
    assert frame["evidence_id"].n_unique() == 3
    manifest = session.scalar(
        select(func.sum(models.ArchiveManifest.row_count))
    )
    assert manifest == 3


def test_manifest_records_sha256_of_stored_bytes(session, tmp_path):
    """M3: manifest.sha256 matches the SHA-256 of the exact stored object."""
    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    _seed_evidence(session, days_ago=11.0, count=2, batch="a")
    RawEvidenceCompactor(store=store, settings=settings).compact(session, DECISION_TS)
    session.commit()

    key = _partition_key(store)
    expected = hashlib.sha256((tmp_path / key).read_bytes()).hexdigest()
    manifest = session.scalars(select(models.ArchiveManifest)).one()
    assert manifest.sha256 == expected


def test_post_write_tamper_is_rejected_and_rows_stay_unarchived(session, tmp_path, monkeypatch):
    """M3: post-write size verification catches a tampered object."""
    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    _seed_evidence(session, days_ago=11.0, count=1, batch="a")
    session.commit()

    real_put = LocalArchiveStore.put_object_if_absent

    def tampering_put(self, key, data: bytes) -> int:
        written = real_put(self, key, data)
        path = Path(self.root) / key
        with open(path, "r+b") as fh:
            fh.truncate(len(data) - 1)
        return written

    monkeypatch.setattr(LocalArchiveStore, "put_object_if_absent", tampering_put)
    with pytest.raises(ArchiveWriteError):
        RawEvidenceCompactor(store=store, settings=settings).compact(session, DECISION_TS)
    session.rollback()

    unarchived = session.scalars(
        select(models.RawEvidenceItem).where(models.RawEvidenceItem.archived_at.is_(None))
    ).all()
    assert len(unarchived) == 1


def test_post_write_same_size_corruption_is_rejected(session, tmp_path, monkeypatch):
    """M3: same-size corruption passes the size check, so only the read-back
    SHA-256 verification can catch it — the pass must fail, not record a
    manifest for corrupt bytes."""
    settings = _settings(tmp_path)
    store = LocalArchiveStore(tmp_path)
    _seed_evidence(session, days_ago=11.0, count=1, batch="a")
    session.commit()

    real_put = LocalArchiveStore.put_object_if_absent

    def corrupting_put(self, key, data: bytes) -> int:
        written = real_put(self, key, data)
        path = Path(self.root) / key
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0xFF  # same size, different bytes
        path.write_bytes(bytes(raw))
        return written

    monkeypatch.setattr(LocalArchiveStore, "put_object_if_absent", corrupting_put)
    with pytest.raises(ArchiveWriteError, match="hash mismatch"):
        RawEvidenceCompactor(store=store, settings=settings).compact(session, DECISION_TS)
    session.rollback()

    unarchived = session.scalars(
        select(models.RawEvidenceItem).where(models.RawEvidenceItem.archived_at.is_(None))
    ).all()
    assert len(unarchived) == 1
