"""Tests for the launcher's backup retention (``scripts/prune_backups.py``).

``start-engine.sh`` writes a verified snapshot on every launch; without a bound
the backup directory grows forever. The retention step keeps the newest N per
database and must never touch a file the launcher did not write.
"""

from __future__ import annotations

from pathlib import Path

from scripts import prune_backups
from scripts.prune_backups import list_snapshots, main, prune, snapshot_regex


def _snapshots(directory: Path, names: list[str]) -> None:
    for name in names:
        (directory / name).write_bytes(b"sqlite")


def test_keeps_newest_n_and_removes_the_rest(tmp_path) -> None:
    names = [f"serpent-2026010{day}-0{day}0000.db" for day in range(1, 6)]
    _snapshots(tmp_path, names)

    kept, removed = prune(tmp_path, database="serpent.db", keep=2)

    assert [path.name for path in kept] == sorted(names, reverse=True)[:2]
    assert {path.name for path in removed} == set(names[:3])
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(path.name for path in kept)


def test_keep_larger_than_count_removes_nothing(tmp_path) -> None:
    names = ["serpent-20260101-010000.db", "serpent-20260102-010000.db"]
    _snapshots(tmp_path, names)

    kept, removed = prune(tmp_path, database="serpent.db", keep=5)

    assert {path.name for path in kept} == set(names)
    assert removed == []


def test_unrelated_files_are_left_alone(tmp_path) -> None:
    _snapshots(
        tmp_path,
        [
            "serpent-20260101-010000.db",
            "serpent-20260102-010000.db",
            "serpent-20260103-010000.db",
            "serpent.db",
            "serpent-20260101-010000.db-wal",
            "other-20260101-010000.db",
            ".venv.old-1794567534",
            "hand-copied-dump.db",
        ],
    )

    kept, removed = prune(tmp_path, database="serpent.db", keep=1)

    assert [path.name for path in kept] == ["serpent-20260103-010000.db"]
    # ``removed`` follows the newest-first scan order the prune uses.
    assert [path.name for path in removed] == [
        "serpent-20260102-010000.db",
        "serpent-20260101-010000.db",
    ]
    survivors = {path.name for path in tmp_path.iterdir()}
    assert {
        "serpent.db",
        "serpent-20260101-010000.db-wal",
        "other-20260101-010000.db",
        ".venv.old-1794567534",
        "hand-copied-dump.db",
    } <= survivors


def test_missing_directory_is_not_an_error(tmp_path) -> None:
    assert list_snapshots(tmp_path / "absent", "serpent.db") == []
    assert prune(tmp_path / "absent", database="serpent.db", keep=3) == ([], [])


def test_other_database_names_are_scoped_separately(tmp_path) -> None:
    _snapshots(
        tmp_path,
        ["serpent-20260101-010000.db", "engine-20260101-010000.db", "engine-20260102-010000.db"],
    )

    kept, _ = prune(tmp_path, database="engine.db", keep=1)

    assert [path.name for path in kept] == ["engine-20260102-010000.db"]
    assert (tmp_path / "serpent-20260101-010000.db").exists()


def test_snapshot_pattern_matches_launcher_names_only() -> None:
    pattern = snapshot_regex("serpent.db")
    assert pattern.match("serpent-20261113-071046.db")
    assert not pattern.match("serpent.db")
    assert not pattern.match("serpent-20261113-071046.db-wal")
    assert not pattern.match("serpent-pre-rescore.db")


def test_cli_prunes_with_keep_flag(tmp_path, capsys) -> None:
    names = [f"serpent-2026010{day}-0{day}0000.db" for day in range(1, 4)]
    _snapshots(tmp_path, names)

    assert main(["--dir", str(tmp_path), "--keep", "1"]) == 0
    out = capsys.readouterr().out
    assert "kept 1 newest of 3" in out and "removed 2" in out
    assert [path.name for path in tmp_path.iterdir()] == [names[-1]]


def test_cli_default_keep_comes_from_the_environment(tmp_path, monkeypatch) -> None:
    names = [f"serpent-2026010{day}-0{day}0000.db" for day in range(1, 5)]
    _snapshots(tmp_path, names)
    monkeypatch.setenv(prune_backups.KEEP_ENV_VAR, "2")

    assert main(["--dir", str(tmp_path)]) == 0
    assert len(list(tmp_path.iterdir())) == 2


def test_cli_rejects_non_positive_keep(tmp_path) -> None:
    try:
        main(["--dir", str(tmp_path), "--keep", "0"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - a zero keep would delete the snapshot just taken
        raise AssertionError("--keep 0 was accepted")


def test_cli_rejects_non_numeric_environment_keep(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(prune_backups.KEEP_ENV_VAR, "lots")
    try:
        main(["--dir", str(tmp_path)])
    except SystemExit as exc:
        assert "positive integer" in str(exc)
    else:  # pragma: no cover - a typo must not silently disable retention
        raise AssertionError("non-numeric keep was accepted")
