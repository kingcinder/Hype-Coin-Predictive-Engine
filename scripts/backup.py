"""Backup sidecar — consistent SQLite + Parquet archive snapshots.

Snapshots the hot SQLite database (via the ``sqlite3`` online backup API, so
the copy is transactionally consistent even while the engine writes) and the
Parquet archive lake into a timestamped tarball under ``BACKUP_DIR``.  Old
snapshots are pruned after ``BACKUP_RETENTION_DAYS`` (default 7).

Designed to run as the ``backup`` sidecar service in docker-compose (mounting
the same data volume read-write and a dedicated backups volume), or standalone:

    python scripts/backup.py                 # one snapshot, then exit
    python scripts/backup.py --loop          # snapshot every BACKUP_INTERVAL_HOURS
    python scripts/backup.py --list          # show existing snapshots

Exit codes: 0 success, 1 failure (a failed backup surfaces via the sidecar's
restart policy rather than silently passing).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sqlite3
import sys
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

DB_PATH = Path(os.getenv("BACKUP_DB_PATH", "serpent.db"))
ARCHIVE_DIR = Path(os.getenv("BACKUP_ARCHIVE_DIR", "data/archive"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "data/backups"))
RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "7"))
INTERVAL_HOURS = float(os.getenv("BACKUP_INTERVAL_HOURS", "24"))

# Must match ops/archive.py::ARCHIVE_MERGE_LOCK_NAME — the lock *filename* is
# the cross-process contract between the compactor and this sidecar. This
# script stays stdlib-only (importing ops.archive would bind the configured
# DB engine at import time via storage.database), so the tiny lock
# implementation is duplicated here rather than imported.
_ARCHIVE_MERGE_LOCK_NAME = ".archive.merge.lock"


@contextlib.contextmanager
def _archive_merge_lock(timeout: float):
    """Hold the compactor's merge lock, waiting at most ``timeout`` seconds.

    Raises ``TimeoutError`` when the lock cannot be acquired in time.
    """
    import fcntl

    path = ARCHIVE_DIR.resolve() / _ARCHIVE_MERGE_LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(path, "a+b") as fh:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out after {timeout}s waiting for archive merge lock {path}"
                    ) from None
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _snapshot_db(dest: Path) -> int:
    """Copy the SQLite DB into ``dest`` using the online backup API.

    Returns the number of bytes copied.  The backup API captures a consistent
    point-in-time image even with concurrent WAL writers, and the resulting
    file is a standalone, openable database.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"database not found: {DB_PATH}")
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)  # online backup: safe under live writes
    finally:
        dst.close()
        src.close()
    return dest.stat().st_size


def _tar_archive(dest: Path) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(str(ARCHIVE_DIR), arcname=ARCHIVE_DIR.name)


def _snapshot_archive(dest: Path) -> int:
    """Tar the Parquet archive lake into ``dest`` (gzip). Returns bytes.

    Takes the compactor's merge lock first so the tar never races a
    partition rewrite: with the compactor quiesced, every archived file the
    tar sees is whole. Waits up to 10 minutes; on timeout it proceeds with a
    warning — a torn *file* is still impossible (the compactor writes via
    atomic rename), but the DB snapshot and the lake may then describe
    slightly different moments.
    """
    if not ARCHIVE_DIR.exists():
        return 0
    try:
        with _archive_merge_lock(timeout=600):
            _tar_archive(dest)
    except TimeoutError as exc:
        print(f"WARNING: {exc}; tarring the lake without quiescing the compactor")
        _tar_archive(dest)
    return dest.stat().st_size


def _prune() -> list[str]:
    """Remove snapshots older than the retention window. Returns pruned names."""
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    pruned: list[str] = []
    for item in BACKUP_DIR.iterdir():
        if not item.name.startswith("serpent-backup-"):
            continue
        try:
            ts = datetime.fromisoformat(item.name.removeprefix("serpent-backup-"))
        except ValueError:
            continue
        if ts < cutoff:
            shutil.rmtree(item, ignore_errors=True)
            pruned.append(item.name)
    return pruned


def _snapshot() -> Path:
    """Take one snapshot; returns the snapshot directory."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snap = BACKUP_DIR / f"serpent-backup-{stamp}"
    snap.mkdir(parents=True, exist_ok=True)
    try:
        db_bytes = _snapshot_db(snap / "serpent.db")
        archive_tar = snap / "archive.tar.gz"
        archive_bytes = _snapshot_archive(archive_tar)
        if archive_bytes == 0:
            archive_tar.unlink(missing_ok=True)  # nothing archived, drop the empty tar
    except Exception:
        shutil.rmtree(snap, ignore_errors=True)
        raise
    pruned = _prune()
    print(
        f"backup {snap.name}: db={db_bytes} B, archive={archive_bytes} B, pruned={pruned}",
        flush=True,
    )
    return snap


def _list() -> None:
    snaps = sorted(p.name for p in BACKUP_DIR.iterdir() if p.is_dir())
    if not snaps:
        print("no snapshots yet")
        return
    print("\n".join(snaps))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--loop", action="store_true", help="snapshot every BACKUP_INTERVAL_HOURS forever"
    )
    parser.add_argument("--list", action="store_true", help="list existing snapshots")
    args = parser.parse_args()

    if args.list:
        _list()
        return 0

    try:
        while True:
            _snapshot()
            if not args.loop:
                return 0
            print(f"sleeping {INTERVAL_HOURS}h until next snapshot", flush=True)
            time.sleep(INTERVAL_HOURS * 3600)
    except Exception as exc:  # noqa: BLE001 - the sidecar must surface failures loudly.
        print(f"backup failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
