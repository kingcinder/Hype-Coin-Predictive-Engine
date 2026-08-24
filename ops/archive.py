from __future__ import annotations

import argparse
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import polars as pl
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)


class ArchiveStore(Protocol):
    """Object-storage surface used by the compactor.

    Implemented for local disk (zero-container profile) and MinIO/S3
    (docker profile). ``put_object`` returns the number of bytes written.
    """

    def put_object(self, key: str, data: bytes) -> int: ...
    def object_exists(self, key: str) -> bool: ...
    def list_objects(self, prefix: str) -> list[str]: ...
    def download_to(self, key: str, dest: Path) -> Path: ...


class LocalArchiveStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        # Guard against traversal while keeping the partition layout readable.
        safe = key.replace("..", "_")
        return (self.root / safe).resolve()

    @property
    def _root_resolved(self) -> Path:
        return self.root.resolve()

    def put_object(self, key: str, data: bytes) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return len(data)

    def object_exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_objects(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.is_dir():
            return []
        root = self._root_resolved
        return [
            str(path.relative_to(root)).replace("\\", "/")
            for path in base.rglob("*.parquet")
        ]

    def download_to(self, key: str, dest: Path) -> Path:
        source = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
        return dest


class S3ArchiveStore:
    """MinIO/S3 backend. boto3 is imported lazily so the zero-container
    profile never requires it to be present at runtime."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self.settings.minio_endpoint,
                aws_access_key_id=self.settings.minio_access_key,
                aws_secret_access_key=self.settings.minio_secret_key,
                region_name="us-east-1",
            )
        return self._client

    def put_object(self, key: str, data: bytes) -> int:
        self._get_client().put_object(
            Bucket=self.settings.minio_bucket, Key=key, Body=data
        )
        return len(data)

    def object_exists(self, key: str) -> bool:
        client = self._get_client()
        try:
            client.head_object(Bucket=self.settings.minio_bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - any head failure means missing/unreadable.
            return False

    def list_objects(self, prefix: str) -> list[str]:
        client = self._get_client()
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.settings.minio_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".parquet"):
                    keys.append(key)
        return keys

    def download_to(self, key: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._get_client().download_file(
            Bucket=self.settings.minio_bucket, Key=key, Filename=str(dest)
        )
        return dest


def make_store(settings: Settings) -> ArchiveStore:
    if settings.archive_backend_is_local:
        return LocalArchiveStore(Path(settings.archive_local_dir))
    return S3ArchiveStore(settings)


def _partition_key(source_name: str, year: int, month: int) -> str:
    return f"source={source_name}/year={year:04d}/month={month:02d}"


def _evidence_frame(rows: list[models.RawEvidenceItem]) -> pl.DataFrame:
    # Timestamps are written as naive UTC: they are UTC instants, and a plain
    # TIMESTAMP column lets DuckDB truncate/compare without the optional pytz
    # module (TIMESTAMP WITH TIME ZONE arithmetic pulls it in on some builds).
    def _naive(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value).replace(tzinfo=None)

    return pl.DataFrame(
        {
            "evidence_id": [row.id for row in rows],
            "source_id": [row.source_id for row in rows],
            "source_type": [row.source_type for row in rows],
            "source_tier": [row.source_tier for row in rows],
            "url_hash": [row.url_hash for row in rows],
            "observed_at": [_naive(row.observed_at) for row in rows],
            "effective_at": [_naive(row.effective_at) for row in rows],
            "ingested_at": [_naive(row.ingested_at) for row in rows],
            "raw_path": [row.raw_path for row in rows],
            "content_hash": [row.content_hash for row in rows],
            "payload_json": [json.dumps(row.payload, default=str) for row in rows],
            "partition_year": [ensure_utc(row.observed_at).year for row in rows],
            "partition_month": [ensure_utc(row.observed_at).month for row in rows],
        }
    )


class RawEvidenceCompactor:
    """Compacts raw evidence older than the cutoff into partitioned Parquet.

    Each partition is one object per ``(source, year, month)`` under the
    configured archive prefix. Manifests are idempotent on ``object_key``,
    and rows are marked ``archived_at`` once written, so re-runs never
    duplicate. After compaction, rows older than the retention window that
    are *not* referenced by normalized tables are pruned from the hot DB;
    referenced provenance rows are always kept.
    """

    def __init__(
        self,
        store: ArchiveStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or make_store(self.settings)

    def compact(self, session: Session, decision_ts: datetime | None = None) -> dict[str, Any]:
        decision_ts = ensure_utc(decision_ts or utc_now())
        cutoff = decision_ts - timedelta(hours=self.settings.archive_compact_after_hours)
        rows = session.scalars(
            select(models.RawEvidenceItem)
            .where(
                models.RawEvidenceItem.observed_at < cutoff,
                models.RawEvidenceItem.archived_at.is_(None),
            )
            .order_by(models.RawEvidenceItem.observed_at)
            .limit(self.settings.archive_batch_size)
        ).all()
        if not rows:
            pruned = self._prune(session, decision_ts)
            return {"compacted": 0, "partitions": 0, "pruned": pruned, "cutoff": cutoff}

        source_names = {
            source.id: source.name
            for source in session.scalars(
                select(models.Source).where(
                    models.Source.id.in_({row.source_id for row in rows})
                )
            )
        }
        groups: dict[tuple[str, int, int], list[models.RawEvidenceItem]] = {}
        for row in rows:
            observed = ensure_utc(row.observed_at)
            key = (source_names.get(row.source_id, "unknown"), observed.year, observed.month)
            groups.setdefault(key, []).append(row)

        partitions = 0
        compacted = 0
        for (source_name, year, month), group in groups.items():
            object_key = (
                f"{self.settings.archive_prefix}/{_partition_key(source_name, year, month)}"
                f"/part-0.parquet"
            )
            frame = _evidence_frame(group)
            # Merge with the existing partition file: a second batch landing in
            # the same (source, year, month) must append to the lake, never
            # clobber the rows compacted by an earlier pass.
            if self.store.object_exists(object_key):
                existing = self._read_partition(object_key)
                if existing.height:
                    frame = pl.concat([existing, frame], how="vertical_relaxed")
            buffer = io.BytesIO()
            frame.write_parquet(buffer)
            byte_size = self.store.put_object(object_key, buffer.getvalue())
            merged_rows = frame.height
            observed_times = sorted(ensure_utc(row.observed_at) for row in group)
            manifest = session.scalar(
                select(models.ArchiveManifest).where(
                    models.ArchiveManifest.object_key == object_key
                )
            )
            if manifest:
                manifest.row_count = merged_rows
                manifest.byte_size = byte_size
                manifest.last_observed_at = observed_times[-1]
            else:
                session.add(
                    models.ArchiveManifest(
                        object_key=object_key,
                        source_id=group[0].source_id,
                        partition_year=year,
                        partition_month=month,
                        row_count=merged_rows,
                        byte_size=byte_size,
                        first_observed_at=observed_times[0],
                        last_observed_at=observed_times[-1],
                    )
                )
            for row in group:
                row.archived_at = decision_ts
            partitions += 1
            compacted += len(group)
        session.flush()
        pruned = self._prune(session, decision_ts)
        return {
            "compacted": compacted,
            "partitions": partitions,
            "pruned": pruned,
            "cutoff": cutoff,
        }

    def _read_partition(self, object_key: str) -> pl.DataFrame:
        """Download a partition object and return its rows (empty on failure)."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="serpent_merge_") as tmp:
            try:
                dest = self.store.download_to(object_key, Path(tmp) / "part.parquet")
                return pl.read_parquet(dest)
            except Exception:  # noqa: BLE001 - a corrupt partition must not block the batch.
                log.warning("archive_partition_unreadable", object_key=object_key)
                return pl.DataFrame()

    def _prune(self, session: Session, decision_ts: datetime) -> int:
        retention_cutoff = decision_ts - timedelta(days=self.settings.archive_retention_days)
        referenced = or_(
            exists().where(
                models.MarketSnapshot.raw_evidence_id == models.RawEvidenceItem.id
            ),
            exists().where(
                models.LiquiditySnapshot.raw_evidence_id == models.RawEvidenceItem.id
            ),
            exists().where(models.ContractFlag.evidence_id == models.RawEvidenceItem.id),
            exists().where(models.NewsItem.raw_evidence_id == models.RawEvidenceItem.id),
        )
        rows = session.scalars(
            select(models.RawEvidenceItem).where(
                models.RawEvidenceItem.archived_at.is_not(None),
                models.RawEvidenceItem.observed_at < retention_cutoff,
                ~referenced,
            )
        ).all()
        for row in rows:
            session.delete(row)
        session.flush()
        return len(rows)


def query_archive(
    sql: str,
    store: ArchiveStore | None = None,
    settings: Settings | None = None,
    *,
    prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Runs a DuckDB query over the Parquet lake.

    Local store: files are read in place. S3/MinIO store: objects are
    materialized into a temporary directory first so the query needs no
    httpfs secrets. The data is exposed to the SQL as a ``evidence`` view
    with columns from the compactor (``evidence_id``, ``source_id``,
    ``payload_json``, ``partition_year``, ...). Returns rows as dicts.
    """
    import duckdb

    settings = settings or get_settings()
    store = store or make_store(settings)
    prefix = prefix or settings.archive_prefix
    keys = store.list_objects(prefix)
    if not keys:
        return []

    import tempfile

    files: list[str] = []
    with tempfile.TemporaryDirectory(prefix="serpent_archive_") as tmp:
        for index, key in enumerate(sorted(keys)):
            dest = Path(tmp) / f"{index:04d}.parquet"
            store.download_to(key, dest)
            files.append(str(dest))
        con = duckdb.connect()
        try:
            con.execute(
                f"CREATE VIEW evidence AS SELECT * FROM read_parquet({files!r}, "
                "union_by_name=true, filename=true)"
            )
            result = con.execute(sql)
            columns = [description[0] for description in result.description]
            return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
        finally:
            con.close()


def run_archive(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.archive_enabled:
        return {"skipped": True}
    decision_ts = ensure_utc(decision_ts or utc_now())
    try:
        result = RawEvidenceCompactor(settings=settings).compact(session, decision_ts)
        record_health(
            session,
            component="archive",
            state="ok",
            message=(
                f"{result['compacted']} rows compacted into {result['partitions']} "
                f"parquet partitions; {result['pruned']} pruned"
            ),
        )
        return result
    except Exception as exc:  # noqa: BLE001 - archive failure must never kill a scan.
        record_health(
            session,
            component="archive",
            state="red",
            message=str(exc),
            error_count=1,
        )
        log.warning("archive_compact_failed", error=str(exc))
        return {"error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Serpent Circle archive & retention jobs")
    parser.add_argument("--once", action="store_true", help="run compaction + prune once")
    parser.add_argument(
        "--query",
        metavar="SQL",
        help="run a DuckDB SQL query over the archived Parquet lake",
    )
    args = parser.parse_args()

    if args.query:
        rows = query_archive(args.query)
        for row in rows:
            print(json.dumps(row, default=str))
        print(f"({len(rows)} rows)")
        return

    if args.once:
        from storage.database import SessionLocal

        settings = get_settings()
        print(
            f"archive backend={settings.archive_backend} "
            f"prefix={settings.archive_prefix} enabled={settings.archive_enabled}"
        )
        with SessionLocal() as session:
            result = run_archive(session)
            session.commit()
        print(json.dumps(result, default=str))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
