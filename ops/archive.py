from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import polars as pl
from sqlalchemy import exists, extract, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)


class PartitionUnreadableError(Exception):
    """A partition object exists but cannot be read/parsed.

    Raised (never swallowed) so a corrupt partition fails the compaction
    pass instead of being silently replaced by only the new batch.
    """


class PartitionConflictError(Exception):
    """A concurrent compactor modified the partition between read and write.

    The merge is retried against the fresh object; if retries are exhausted
    the pass fails rather than dropping either batch.
    """


class ArchiveWriteError(Exception):
    """Post-write verification showed the stored object is not what we wrote."""


@dataclass(frozen=True)
class ObjectStat:
    """Post-write/read identity of a stored object."""

    size: int
    etag: str | None


# Name of the inter-process lock file inside an archive root. The compactor
# holds it (exclusive) for the whole merge pass; the backup sidecar holds it
# while tarring the lake, so a backup never races a partition rewrite.
ARCHIVE_MERGE_LOCK_NAME = ".archive.merge.lock"


@contextlib.contextmanager
def archive_merge_lock(root: Path | str, timeout: float | None = None) -> Iterator[None]:
    """Exclusive inter-process lock for archive-root mutations.

    ``timeout=None`` blocks until acquired; otherwise raises ``TimeoutError``
    after ``timeout`` seconds. Implemented with ``fcntl.flock`` (Linux/macOS
    deployment targets).
    """
    import fcntl

    path = Path(root).resolve() / ARCHIVE_MERGE_LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = None if timeout is None else time.monotonic() + timeout
    with open(path, "a+b") as fh:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out after {timeout}s waiting for archive merge lock {path}"
                    ) from None
                time.sleep(0.25)
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class ArchiveStore(Protocol):
    """Object-storage surface used by the compactor.

    Implemented for local disk (zero-container profile) and MinIO/S3
    (docker profile). ``put_object`` returns the number of bytes written.
    """

    def put_object(self, key: str, data: bytes) -> int: ...
    def put_object_if_absent(self, key: str, data: bytes) -> int: ...
    def object_exists(self, key: str) -> bool: ...
    def read_object(self, key: str) -> bytes: ...
    def list_objects(self, prefix: str) -> list[str]: ...
    def download_to(self, key: str, dest: Path) -> Path: ...
    def merge_lock(self) -> contextlib.AbstractContextManager[None]: ...
    def stat_object(self, key: str) -> ObjectStat | None: ...
    def put_object_if_match(self, key: str, data: bytes, etag: str) -> int: ...


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
        # Atomic write: temp file + fsync + rename, so a kill/OOM/disk-full
        # mid-write can never leave a partial Parquet file behind for the
        # next compaction pass to choke on (defect H1).
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return len(data)

    def put_object_if_absent(self, key: str, data: bytes) -> int:
        # Atomic conditional create: temp file + fsync + hard link. os.link
        # raises FileExistsError when the target already exists, so the
        # create-if-absent is atomic against concurrent creators — the
        # initial-object half of the H2 read-modify-write race.
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.new"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise PartitionConflictError(
                    f"partition {key} created concurrently"
                ) from None
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            tmp.unlink(missing_ok=True)
        return len(data)

    def object_exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def read_object(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def merge_lock(self) -> contextlib.AbstractContextManager[None]:
        return archive_merge_lock(self.root)

    def stat_object(self, key: str) -> ObjectStat | None:
        try:
            stat = self._path(key).stat()
        except FileNotFoundError:
            return None
        return ObjectStat(size=stat.st_size, etag=f"{stat.st_mtime_ns:x}:{stat.st_size:x}")

    def put_object_if_match(self, key: str, data: bytes, etag: str) -> int:
        # Under merge_lock() the object cannot change between stat and write,
        # but the check is enforced anyway so a lock-bypassing writer can
        # never silently win a read-modify-write race.
        current = self.stat_object(key)
        if current is None or current.etag != etag:
            raise PartitionConflictError(f"partition {key} changed during merge")
        return self.put_object(key, data)

    def list_objects(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.is_dir():
            return []
        root = self._root_resolved
        return [str(path.relative_to(root)).replace("\\", "/") for path in base.rglob("*.parquet")]

    def download_to(self, key: str, dest: Path) -> Path:
        source = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
        return dest


def _s3_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return str(error.get("Code", ""))


def _is_not_found(exc: Exception) -> bool:
    """True when ``exc`` is an S3 404/NoSuchKey-style "object does not exist"."""
    return _s3_error_code(exc) in {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}


def _is_precondition_failed(exc: Exception) -> bool:
    """True when ``exc`` is an S3 412 PreconditionFailed (If-Match miss)."""
    if _s3_error_code(exc) == "PreconditionFailed":
        return True
    response = getattr(exc, "response", None) or {}
    metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    return metadata.get("HTTPStatusCode") == 412


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
        self._get_client().put_object(Bucket=self.settings.minio_bucket, Key=key, Body=data)
        return len(data)

    def object_exists(self, key: str) -> bool:
        client = self._get_client()
        try:
            client.head_object(Bucket=self.settings.minio_bucket, Key=key)
            return True
        except Exception as exc:  # noqa: BLE001
            # Fail closed: only a genuine 404 means "missing". Any other head
            # failure (permissions, network, 500) must NOT be treated as
            # missing, or the merge would overwrite an existing partition with
            # only the new batch (defect C2, S3 variant).
            if _is_not_found(exc):
                return False
            raise

    def merge_lock(self) -> contextlib.AbstractContextManager[None]:
        # No cross-process lock primitive on plain S3; concurrent merges are
        # serialized by ETag conditional writes (put_object_if_match).
        return contextlib.nullcontext()

    def stat_object(self, key: str) -> ObjectStat | None:
        client = self._get_client()
        try:
            head = client.head_object(Bucket=self.settings.minio_bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise
        etag = head.get("ETag")
        # Keep the ETag exactly as HEAD returned it (quotes included): the
        # If-Match precondition compares the opaque value canonically.
        return ObjectStat(
            size=int(head.get("ContentLength", -1)),
            etag=etag if etag else None,
        )

    def put_object_if_absent(self, key: str, data: bytes) -> int:
        # S3 conditional create (IfNoneMatch="*"): atomic against concurrent
        # creators — the initial-object half of the H2 read-modify-write race.
        client = self._get_client()
        try:
            client.put_object(
                Bucket=self.settings.minio_bucket,
                Key=key,
                Body=data,
                IfNoneMatch="*",
            )
        except Exception as exc:  # noqa: BLE001
            if _is_precondition_failed(exc):
                raise PartitionConflictError(
                    f"partition {key} created concurrently"
                ) from exc
            raise
        return len(data)

    def put_object_if_match(self, key: str, data: bytes, etag: str) -> int:
        client = self._get_client()
        try:
            client.put_object(
                Bucket=self.settings.minio_bucket, Key=key, Body=data, IfMatch=etag
            )
        except Exception as exc:  # noqa: BLE001
            if _is_precondition_failed(exc):
                raise PartitionConflictError(
                    f"partition {key} changed during merge"
                ) from exc
            raise
        return len(data)

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

    def read_object(self, key: str) -> bytes:
        import io

        buf = io.BytesIO()
        self._get_client().download_fileobj(
            Bucket=self.settings.minio_bucket, Key=key, Fileobj=buf
        )
        return buf.getvalue()


def make_store(settings: Settings) -> ArchiveStore:
    if settings.archive_backend_is_local:
        return LocalArchiveStore(Path(settings.archive_local_dir))
    return S3ArchiveStore(settings)


def _partition_key(source_name: str, year: int, month: int) -> str:
    return f"source={source_name}/year={year:04d}/month={month:02d}"


def due_partitions(
    session: Session,
    decision_ts: datetime | None = None,
    settings: Settings | None = None,
) -> list[tuple[int, int, int]]:
    """Partitions ``(source_id, year, month)`` due for compaction.

    A partition is due when it holds unarchived raw evidence whose
    ``observed_at`` has aged past ``ARCHIVE_COMPACT_AFTER_HOURS``. This is the
    per-partition compaction schedule: the retention autopilot computes it on
    each cadence pass and compacts exactly these partitions — a pass with no
    due partitions does zero compaction work.
    """
    settings = settings or get_settings()
    decision_ts = ensure_utc(decision_ts or utc_now())
    cutoff = decision_ts - timedelta(hours=settings.archive_compact_after_hours)
    rows = session.execute(
        select(
            models.RawEvidenceItem.source_id,
            extract("year", models.RawEvidenceItem.observed_at),
            extract("month", models.RawEvidenceItem.observed_at),
        )
        .where(
            models.RawEvidenceItem.observed_at < cutoff,
            models.RawEvidenceItem.archived_at.is_(None),
        )
        .distinct()
    ).all()
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


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

    def compact(
        self,
        session: Session,
        decision_ts: datetime | None = None,
        *,
        partition_filter: set[tuple[int, int, int]] | None = None,
    ) -> dict[str, Any]:
        """Compact raw evidence older than the cutoff into due partitions.

        ``partition_filter`` restricts compaction to the given ``(source_id,
        year, month)`` partitions — the per-partition schedule computed by
        :func:`due_partitions`. An empty filter skips the batch entirely but
        still prunes expired rows.
        """
        decision_ts = ensure_utc(decision_ts or utc_now())
        cutoff = decision_ts - timedelta(hours=self.settings.archive_compact_after_hours)
        if partition_filter is not None and not partition_filter:
            # Nothing due on the per-partition schedule: no compaction work.
            pruned = self._prune(session, decision_ts)
            return {
                "compacted": 0,
                "partitions": 0,
                "pruned": pruned,
                "cutoff": cutoff,
                "due_partitions": 0,
            }
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
            return {
                "compacted": 0,
                "partitions": 0,
                "pruned": pruned,
                "cutoff": cutoff,
                "due_partitions": 0,
            }

        source_names = {
            source.id: source.name
            for source in session.scalars(
                select(models.Source).where(models.Source.id.in_({row.source_id for row in rows}))
            )
        }
        groups: dict[tuple[int, int, int], list[models.RawEvidenceItem]] = {}
        for row in rows:
            observed = ensure_utc(row.observed_at)
            key = (row.source_id, observed.year, observed.month)
            if partition_filter is not None and key not in partition_filter:
                continue
            groups.setdefault(key, []).append(row)

        partitions = 0
        compacted = 0
        # The whole merge pass holds the archive merge lock: two compactor
        # processes (retention autopilot cadence + a manual --once run) must
        # never interleave read-modify-write cycles on the same partition
        # (defect H2). On S3 the lock is a no-op and each partition merge
        # instead uses an ETag conditional write with retry.
        with self.store.merge_lock():
            for (source_id, year, month), group in groups.items():
                self._compact_partition(
                    session, source_names, source_id, year, month, group, decision_ts
                )
                partitions += 1
                compacted += len(group)
        session.flush()
        pruned = self._prune(session, decision_ts)
        return {
            "compacted": compacted,
            "partitions": partitions,
            "pruned": pruned,
            "cutoff": cutoff,
            "due_partitions": len(groups),
        }

    # Retries for a lost conditional-write race. A conflict means another
    # compactor won the write (either the initial create or an ETag-guarded
    # merge); re-reading and re-merging converges instead of dropping either
    # batch. Exhaustion raises: the pass fails red, rows stay unarchived,
    # nothing is silently lost.
    _MERGE_RETRIES = 3

    def _compact_partition(
        self,
        session: Session,
        source_names: dict[int, str],
        source_id: int,
        year: int,
        month: int,
        group: list[models.RawEvidenceItem],
        decision_ts: datetime,
    ) -> None:
        source_name = source_names.get(source_id, "unknown")
        object_key = (
            f"{self.settings.archive_prefix}/{_partition_key(source_name, year, month)}"
            f"/part-0.parquet"
        )
        new_frame = _evidence_frame(group)
        last_error: Exception | None = None
        for attempt in range(self._MERGE_RETRIES):
            try:
                self._merge_one_partition(
                    session, object_key, source_id, year, month, new_frame, group, decision_ts
                )
                return
            except PartitionConflictError as exc:
                last_error = exc
                log.warning(
                    "archive_partition_conflict_retry",
                    object_key=object_key,
                    attempt=attempt + 1,
                )
        assert last_error is not None
        raise last_error

    def _merge_one_partition(
        self,
        session: Session,
        object_key: str,
        source_id: int,
        year: int,
        month: int,
        new_frame: pl.DataFrame,
        group: list[models.RawEvidenceItem],
        decision_ts: datetime,
    ) -> None:
        # Merge with the existing partition file: a second batch landing in
        # the same (source, year, month) must append to the lake, never
        # clobber the rows compacted by an earlier pass.
        stat = self.store.stat_object(object_key)
        if stat is not None:
            # Raises PartitionUnreadableError on any read failure: a corrupt
            # partition fails the pass instead of being amputated by the new
            # batch alone (defect C2).
            existing = self._read_partition(object_key)
            frame = pl.concat([existing, new_frame], how="vertical_relaxed")
            # Idempotent merge (defect M1): if a previous PUT succeeded but
            # the DB commit failed, the rows stayed archived_at=NULL and this
            # pass re-selects them — dedup on evidence_id so the re-merge
            # cannot duplicate them in the lake.
            frame = frame.unique(subset=["evidence_id"], keep="first", maintain_order=True)
        else:
            frame = new_frame
        buffer = io.BytesIO()
        frame.write_parquet(buffer)
        payload = buffer.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        if stat is not None and stat.etag is not None:
            byte_size = self.store.put_object_if_match(object_key, payload, stat.etag)
        elif stat is not None:
            # No ETag available (should not happen for the S3 backend, but
            # stay safe): unconditional write would race, so treat as a
            # conflict and let the retry loop re-stat.
            raise PartitionConflictError(
                f"partition {object_key} has no ETag for conditional write"
            )
        else:
            # Conditional create: another compactor creating this partition
            # between our stat and this write raises PartitionConflictError
            # and the retry loop merges on top of their object instead of
            # clobbering it (initial-object half of defect H2).
            byte_size = self.store.put_object_if_absent(object_key, payload)
        # Verify what was actually stored before trusting it (defect M3):
        # the manifest must describe the bytes on the object, not the bytes
        # we intended to write.
        written = self.store.stat_object(object_key)
        if written is None or written.size != len(payload):
            raise ArchiveWriteError(
                f"post-write verification failed for {object_key}: "
                f"wrote {len(payload)} bytes, stored "
                f"{written.size if written else 'nothing'}"
            )
        # Read-back hash check: size alone cannot catch same-size corruption
        # (bit-rot, broken transfer). The manifest's sha256 must describe the
        # stored bytes, not just the intended payload.
        stored = self.store.read_object(object_key)
        if hashlib.sha256(stored).hexdigest() != digest:
            raise ArchiveWriteError(
                f"post-write hash mismatch for {object_key}: stored bytes do "
                "not match the payload that was written"
            )
        self._upsert_manifest(
            session, object_key, source_id, year, month, group,
            row_count=frame.height, byte_size=byte_size, digest=digest,
        )
        for row in group:
            row.archived_at = decision_ts

    def _upsert_manifest(
        self,
        session: Session,
        object_key: str,
        source_id: int,
        year: int,
        month: int,
        group: list[models.RawEvidenceItem],
        *,
        row_count: int,
        byte_size: int,
        digest: str,
    ) -> None:
        observed_times = sorted(ensure_utc(row.observed_at) for row in group)

        def _apply(manifest: models.ArchiveManifest) -> None:
            manifest.row_count = row_count
            manifest.byte_size = byte_size
            manifest.sha256 = digest
            manifest.last_observed_at = observed_times[-1]

        try:
            with session.begin_nested():
                manifest = session.scalar(
                    select(models.ArchiveManifest).where(
                        models.ArchiveManifest.object_key == object_key
                    )
                )
                if manifest is not None:
                    _apply(manifest)
                else:
                    session.add(
                        models.ArchiveManifest(
                            object_key=object_key,
                            source_id=source_id,
                            partition_year=year,
                            partition_month=month,
                            row_count=row_count,
                            byte_size=byte_size,
                            sha256=digest,
                            first_observed_at=observed_times[0],
                            last_observed_at=observed_times[-1],
                        )
                    )
                session.flush()
        except IntegrityError:
            # Lost a concurrent insert race on uq_archive_manifest_object_key:
            # the winner's row is now committed and visible — update it
            # instead of failing the pass (defect H2, manifest half).
            log.warning("archive_manifest_insert_race", object_key=object_key)
            manifest = session.scalar(
                select(models.ArchiveManifest).where(
                    models.ArchiveManifest.object_key == object_key
                )
            )
            if manifest is None:
                raise
            _apply(manifest)
            session.flush()

    def _read_partition(self, object_key: str) -> pl.DataFrame:
        """Download a partition object and return its rows.

        Raises :class:`PartitionUnreadableError` on any failure: the merge
        must fail closed (the pass errors, rows stay unarchived) rather than
        treat the unreadable partition as empty and permanently replace it
        with only the new batch (defect C2).
        """
        import tempfile

        with tempfile.TemporaryDirectory(prefix="serpent_merge_") as tmp:
            try:
                dest = self.store.download_to(object_key, Path(tmp) / "part.parquet")
                return pl.read_parquet(dest)
            except Exception as exc:
                log.warning("archive_partition_unreadable", object_key=object_key, error=str(exc))
                raise PartitionUnreadableError(
                    f"cannot read archive partition {object_key}: {exc}"
                ) from exc

    def _prune(self, session: Session, decision_ts: datetime) -> int:
        retention_cutoff = decision_ts - timedelta(days=self.settings.archive_retention_days)
        referenced = or_(
            exists().where(models.MarketSnapshot.raw_evidence_id == models.RawEvidenceItem.id),
            exists().where(models.LiquiditySnapshot.raw_evidence_id == models.RawEvidenceItem.id),
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
            try:
                store.download_to(key, dest)
            except Exception as exc:  # noqa: BLE001 - one unreadable object must not hide the lake.
                log.warning("archive_object_unreadable", object_key=key, error=str(exc))
                continue
            files.append(str(dest))
        if not files:
            return []
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
    partition_filter: set[tuple[int, int, int]] | None = None,
) -> dict[str, Any]:
    """Compact due partitions (per-partition schedule) and record archive health.

    ``partition_filter`` is the per-partition schedule; when omitted the pass
    compacts whatever is older than the cutoff (backward-compatible fallback
    for ``python -m ops.archive --once``)."""
    settings = settings or get_settings()
    if not settings.archive_enabled:
        return {"skipped": True}
    decision_ts = ensure_utc(decision_ts or utc_now())
    try:
        result = RawEvidenceCompactor(settings=settings).compact(
            session, decision_ts, partition_filter=partition_filter
        )
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
