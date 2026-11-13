"""Tests for the lock-sync guard (``scripts/check_lock_sync.py``).

The guard's job: a dependency added to ``pyproject.toml`` without a matching pin
in ``requirements-dev.txt`` (or a pin that drifts outside its declared range)
must fail the lint suite instead of surfacing at install time.
"""

from __future__ import annotations

from pathlib import Path

from scripts import check_lock_sync
from scripts.check_lock_sync import _lock_sync_issues, main


def _write(tmp_path: Path, *, pyproject: str, lock: str) -> tuple[Path, Path]:
    pyproject_path = tmp_path / "pyproject.toml"
    lock_path = tmp_path / "requirements-dev.txt"
    pyproject_path.write_text(pyproject, encoding="utf-8")
    lock_path.write_text(lock, encoding="utf-8")
    return pyproject_path, lock_path


def _pyproject(deps: str, dev: str = "") -> str:
    return (
        "[project]\n"
        'name = "example"\n'
        'version = "0.1.0"\n'
        f"dependencies = [{deps}]\n"
        "\n[project.optional-dependencies]\n"
        f"dev = [{dev}]\n"
    )


def test_clean_lock_has_no_issues(tmp_path) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"fastapi>=0.115"', '"pytest>=8.3"'),
        lock="fastapi==0.141.1\npytest==9.1.1\n",
    )
    assert _lock_sync_issues(pyproject, lock) == []


def test_missing_declaration_in_lock_is_reported(tmp_path) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"fastapi>=0.115", "defusedxml>=0.7.1"', '"pytest>=8.3"'),
        lock="fastapi==0.141.1\npytest==9.1.1\n",
    )
    issues = _lock_sync_issues(pyproject, lock)
    assert len(issues) == 1
    assert "defusedxml" in issues[0] and "no pin" in issues[0]


def test_dev_extra_is_covered_by_default(tmp_path) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"fastapi>=0.115"', '"mypy>=1.10"'),
        lock="fastapi==0.141.1\n",
    )
    issues = _lock_sync_issues(pyproject, lock)
    assert any("mypy" in issue for issue in issues)


def test_pin_below_floor_is_reported(tmp_path) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"pytest>=8.3"'),
        lock="pytest==7.4.4\n",
    )
    issues = _lock_sync_issues(pyproject, lock)
    assert len(issues) == 1
    assert "pytest==7.4.4" in issues[0] and "outside the declared range" in issues[0]


def test_pin_above_declared_ceiling_is_reported(tmp_path) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"sqlalchemy>=2.0,<2.1"'),
        lock="SQLAlchemy==2.1.8\n",
    )
    issues = _lock_sync_issues(pyproject, lock)
    assert len(issues) == 1
    assert "2.1.8" in issues[0]


def test_compatible_release_operator(tmp_path) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"httpx~=0.27.0"'),
        lock="httpx==0.27.2\n",
    )
    assert _lock_sync_issues(pyproject, lock) == []
    lock.write_text("httpx==0.28.0\n", encoding="utf-8")
    assert any("httpx==0.28.0" in issue for issue in _lock_sync_issues(pyproject, lock))


def test_unpinned_lock_line_is_reported(tmp_path) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"fastapi>=0.115"'),
        lock="fastapi>=0.115\n",
    )
    issues = _lock_sync_issues(pyproject, lock)
    assert any("not pinned" in issue for issue in issues)
    assert any("fastapi" in issue for issue in issues)


def test_unsupported_specifier_is_reported_not_passed(tmp_path) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"fastapi==0.1.*"'),
        lock="fastapi==0.115.0\n",
    )
    issues = _lock_sync_issues(pyproject, lock)
    assert any("cannot be checked" in issue for issue in issues)


def test_main_reports_ok_and_failure(tmp_path, capsys) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"fastapi>=0.115"'),
        lock="fastapi==0.141.1\n",
    )
    assert main(["--pyproject", str(pyproject), "--lock", str(lock)]) == 0
    assert "lock-sync scan OK" in capsys.readouterr().out

    lock.write_text("fastapi==0.1\n", encoding="utf-8")
    assert main(["--pyproject", str(pyproject), "--lock", str(lock)]) == 1
    assert "lock-sync scan FAILED" in capsys.readouterr().out


def test_unknown_extra_is_rejected(tmp_path) -> None:
    pyproject, lock = _write(
        tmp_path,
        pyproject=_pyproject('"fastapi>=0.115"'),
        lock="fastapi==0.141.1\n",
    )
    try:
        check_lock_sync._declared_requirements(pyproject, ("nope",))
    except SystemExit as exc:
        assert "no optional-dependencies group" in str(exc)
    else:  # pragma: no cover - the guard must never silently accept a typo
        raise AssertionError("unknown group was accepted")


def test_repository_lock_is_in_sync() -> None:
    """The committed lock must match the committed declarations."""
    repo = Path(__file__).resolve().parents[1]
    assert _lock_sync_issues(repo / "pyproject.toml", repo / "requirements-dev.txt") == []
