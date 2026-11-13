"""Keep only the newest N snapshots per database in the launcher's backup dir.

``start-engine.sh`` writes a verified ``sqlite3`` backup of ``serpent.db`` into
``~/serpent-backups/`` before every launch (or ``SERPENT_BACKUP_DIR``). That is
the right safety net before a migration, but nothing bounded the directory, so
every launch added another ~60 MB snapshot forever.

This is that bound: after each successful backup the launcher calls

    python3 scripts/prune_backups.py --dir "$BACKUP_DIR" --database serpent.db

which keeps the newest ``--keep`` snapshots of that database (default
``SERPENT_BACKUP_KEEP``, else 10) and deletes the rest. It only ever touches
files the launcher itself wrote — ``<stem>-YYYYmmdd-HHMMSS.db``, where ``<stem>``
is the database's basename without ``.db`` — so unrelated files in the directory
(an older ``.venv.old-*``, a hand-copied dump with another name) are left alone.

Stdlib-only on purpose: it runs before the venv exists, with the system
``python3`` the launcher already uses for the backup itself.

Usage:
    python scripts/prune_backups.py --dir ~/serpent-backups
    python scripts/prune_backups.py --dir ~/serpent-backups --keep 3
    SERPENT_BACKUP_KEEP=5 python scripts/prune_backups.py --dir ~/serpent-backups
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

#: Snapshots kept per database when neither ``--keep`` nor the environment says.
DEFAULT_KEEP = 10

#: Environment override shared with ``start-engine.sh``.
KEEP_ENV_VAR = "SERPENT_BACKUP_KEEP"


def snapshot_regex(database: str) -> re.Pattern[str]:
    """Pattern for the launcher's snapshots of ``database``.

    ``serpent.db`` -> ``serpent-YYYYmmdd-HHMMSS.db``. The timestamp is fixed
    width, so sorting the names sorts them chronologically.
    """
    stem = re.escape(Path(database).stem)
    return re.compile(rf"^{stem}-\d{{8}}-\d{{6}}\.db$")


def list_snapshots(directory: Path, database: str) -> list[Path]:
    """Snapshots of ``database`` in ``directory``, newest first.

    An absent directory means no backup has been taken yet — not an error.
    """
    if not directory.is_dir():
        return []
    pattern = snapshot_regex(database)
    return sorted(
        (path for path in directory.iterdir() if path.is_file() and pattern.match(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )


def prune(directory: Path, *, database: str, keep: int) -> tuple[list[Path], list[Path]]:
    """Keep the newest ``keep`` snapshots of ``database``; return (kept, removed)."""
    if keep < 1:
        raise ValueError(f"keep must be at least 1 (got {keep})")
    snapshots = list_snapshots(directory, database)
    kept = snapshots[:keep]
    removed: list[Path] = []
    for stale in snapshots[keep:]:
        stale.unlink()
        removed.append(stale)
    return kept, removed


def _keep_from_args(value: int | None) -> int:
    """``--keep`` wins, then ``SERPENT_BACKUP_KEEP``, then the default."""
    if value is not None:
        return value
    raw = os.environ.get(KEEP_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_KEEP
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{KEEP_ENV_VAR} must be a positive integer (got {raw!r})") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Keep the newest N start-engine.sh snapshots per database and delete the older ones"
        )
    )
    parser.add_argument(
        "--dir",
        default=str(Path.home() / "serpent-backups"),
        metavar="PATH",
        help="backup directory (default: ~/serpent-backups, the launcher's default)",
    )
    parser.add_argument(
        "--database",
        default="serpent.db",
        metavar="NAME",
        help="database whose snapshots to prune (default: serpent.db)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        metavar="N",
        help=(f"newest snapshots to keep (default: $SERPENT_BACKUP_KEEP, else {DEFAULT_KEEP})"),
    )
    args = parser.parse_args(argv)

    keep = _keep_from_args(args.keep)
    if keep < 1:
        parser.error(f"--keep must be at least 1 (got {keep})")

    directory = Path(args.dir).expanduser()
    kept, removed = prune(directory, database=args.database, keep=keep)
    if not kept and not removed:
        print(f"no {args.database} snapshots to prune in {directory}")
        return 0
    print(
        f"backups: kept {len(kept)} newest of {len(kept) + len(removed)} "
        f"{args.database} snapshot(s) in {directory} (keep={keep}), "
        f"removed {len(removed)}"
    )
    for stale in removed:
        print(f"  removed {stale.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
