from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select

from common.config import Settings
from ops.archive import (
    LocalArchiveStore,
    RawEvidenceCompactor,
    S3ArchiveStore,
    make_store,
    query_archive,
)
from storage import models
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    store_raw_evidence,
)

DECISION_TS = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
BUCKET = "raw-evidence"


def _settings(**overrides) -> Settings:
    kwargs: dict[str, object] = {
        "archive_enabled": True,
        "archive_backend": "s3",
        "archive_local_dir": "/tmp/serpent-s3-tests",
        "archive_compact_after_hours": 72.0,
        "archive_retention_days": 30,
        "archive_batch_size": 5_000,
        "archive_prefix": "evidence",
        "minio_endpoint": "http://localhost:9000",  # moto intercepts this
        "minio_access_key": "minioadmin",
        "minio_secret_key": "minioadmin",
        "minio_bucket": BUCKET,
    }
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


@pytest.fixture()
def s3(monkeypatch) -> None:
    """Run the test inside moto's in-memory S3 with the bucket pre-created.

    ``S3ArchiveStore`` builds its boto3 client with the configured
    ``MINIO_ENDPOINT``; moto only intercepts clients without an explicit
    endpoint, so the store's client factory is monkeypatched to drop it.
    """
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)

        def _moto_client(self: S3ArchiveStore):
            return boto3.client(
                "s3",
                region_name="us-east-1",
                aws_access_key_id=self.settings.minio_access_key,
                aws_secret_access_key=self.settings.minio_secret_key,
            )

        monkeypatch.setattr(S3ArchiveStore, "_get_client", _moto_client)
        yield


def _seed_evidence(session, *, count: int = 2, days_ago: float = 11.0, batch: str = "a"):
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
    assert chain.id
    return source


def test_s3_store_put_object_and_object_exists(s3) -> None:
    store = S3ArchiveStore(_settings())

    assert store.object_exists("evidence/source=x/year=2026/month=07/part-0.parquet") is False
    assert store.put_object(
        "evidence/source=x/year=2026/month=07/part-0.parquet", b"parquet-bytes"
    ) == len(b"parquet-bytes")
    assert store.object_exists("evidence/source=x/year=2026/month=07/part-0.parquet") is True


def test_s3_store_list_objects_filters_prefix_and_parquet(s3) -> None:
    store = S3ArchiveStore(_settings())
    store.put_object("evidence/source=a/year=2026/month=06/part-0.parquet", b"a")
    store.put_object("evidence/source=b/year=2026/month=07/part-0.parquet", b"b")
    store.put_object("evidence/source=c/year=2026/month=07/part-0.parquet", b"c")
    # Non-parquet and out-of-prefix objects are ignored.
    store.put_object("evidence/source=d/year=2026/month=07/part-0.txt", b"x")
    store.put_object("other-prefix/year=2026/month=07/part-0.parquet", b"y")

    keys = store.list_objects("evidence")
    assert sorted(keys) == [
        "evidence/source=a/year=2026/month=06/part-0.parquet",
        "evidence/source=b/year=2026/month=07/part-0.parquet",
        "evidence/source=c/year=2026/month=07/part-0.parquet",
    ]
    # Prefix narrowing works on a subdirectory.
    assert store.list_objects("evidence/source=b") == [
        "evidence/source=b/year=2026/month=07/part-0.parquet"
    ]
    # Missing prefix lists nothing.
    assert store.list_objects("evidence/does-not-exist") == []


def test_s3_store_download_to_materializes_object(s3, tmp_path) -> None:
    store = S3ArchiveStore(_settings())
    store.put_object("evidence/source=a/year=2026/month=06/part-0.parquet", b"lake-bytes")

    dest = store.download_to(
        "evidence/source=a/year=2026/month=06/part-0.parquet",
        tmp_path / "dl" / "part.parquet",
    )
    assert dest.read_bytes() == b"lake-bytes"


def test_make_store_selects_backend_by_config(s3, tmp_path) -> None:
    assert isinstance(make_store(_settings()), S3ArchiveStore)
    local = _settings(
        archive_backend="local", archive_local_dir=str(tmp_path), minio_bucket=BUCKET
    )
    assert isinstance(make_store(local), LocalArchiveStore)


def test_s3_compactor_writes_partitions_and_query_archive_reads_lake(
    session, s3, tmp_path
) -> None:
    """Full MinIO-style flow: compaction writes partitioned Parquet to S3 and
    the DuckDB query path materializes the objects and returns the rows."""
    source = _seed_evidence(session, count=2, days_ago=11.0)
    _seed_evidence(session, count=1, days_ago=77.0, batch="b")
    settings = _settings()
    store = S3ArchiveStore(settings)

    result = RawEvidenceCompactor(store=store, settings=settings).compact(
        session, DECISION_TS
    )
    session.flush()

    assert result["compacted"] == 3
    assert result["partitions"] == 2  # June + April
    assert len(store.list_objects("evidence")) == 2
    manifests = session.scalars(select(models.ArchiveManifest)).all()
    assert {m.partition_month for m in manifests} == {4, 6}
    assert all(m.object_key.startswith("evidence/source=dexscreener/") for m in manifests)

    rows = query_archive(
        "SELECT count(*) AS n FROM evidence", store=store, settings=settings
    )
    assert rows == [{"n": 3}]

    by_source = query_archive(
        "SELECT source_type, count(*) AS n FROM evidence GROUP BY source_type",
        store=store,
        settings=settings,
    )
    assert by_source == [{"source_type": "market_data", "n": 3}]
    assert source.id  # keep reference for linters
