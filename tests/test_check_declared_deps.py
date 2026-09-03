"""Unit tests for the dependency-declaration guard
(scripts/check_declared_deps.py).

Pins the contract: every third-party import root the repo uses must be
declared in ``pyproject.toml`` (``[project.dependencies]`` or any
``[project.optional-dependencies]`` group) or in ``KNOWN_VENV_ABSENT`` —
stdlib and repo-local roots are always skipped. Snippets are plain strings
written to a synthetic repo (``storage/models.py`` + ``common/enums.py``)
with a synthetic ``pyproject.toml``, so tests are hermetic and never couple
to the live repo layout or its real dependency list.

Mirrors the collection-guard suite's structure: API-level scans, a
guard-the-guard section (every ``KNOWN_VENV_ABSENT`` entry is a REAL
exemption — flagged when removed), and CLI subprocess tests running the REAL
script with ``--path`` / ``--pyproject``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_broken_imports import (
    KNOWN_VENV_ABSENT,
    _repo_root,
)
from scripts.check_declared_deps import (
    _IMPORT_ROOT_ALIASES,
    _requirement_base_name,
    scan_files,
)

# Namespace-package layout the root classifier walks; a top-level name that
# isn't stdlib and doesn't exist under the repo counts as third-party.
_SYNTHETIC_MODULES = ("storage/models.py", "common/enums.py")


def _write_pyproject(
    repo: Path,
    *,
    deps: tuple[str, ...] = (),
    extras: dict[str, tuple[str, ...]] | None = None,
) -> None:
    lines = ["[project]", 'name = "synthetic"', 'version = "0.1.0"', "dependencies = ["]
    lines += [f'  "{d}",' for d in deps]
    lines.append("]\n[project.optional-dependencies]")
    for group, pkgs in (extras or {}).items():
        lines.append(f"{group} = [")
        lines += [f'  "{p}",' for p in pkgs]
        lines.append("]")
    (repo / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_repo(
    tmp_path: Path,
    *,
    deps: tuple[str, ...] = (),
    extras: dict[str, tuple[str, ...]] | None = None,
) -> Path:
    repo = tmp_path / "repo"
    for rel in _SYNTHETIC_MODULES:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    _write_pyproject(repo, deps=deps, extras=extras)
    return repo


def _scan(
    snippet: str,
    tmp_path: Path,
    *,
    deps: tuple[str, ...] = (),
    extras: dict[str, tuple[str, ...]] | None = None,
    filename: str = "mod.py",
) -> list[str]:
    """Scan exactly one temp snippet against a hermetic synthetic repo."""
    repo = _make_repo(tmp_path, deps=deps, extras=extras)
    target = repo / filename
    target.write_text(snippet, encoding="utf-8")
    return scan_files([target], repo, pyproject=repo / "pyproject.toml")


# ---------------------------------------------------------------------------
# Declared roots stay quiet (dependencies table + every extras group)
# ---------------------------------------------------------------------------


def test_declared_in_dependencies_quiet(tmp_path: Path) -> None:
    issues = _scan("import declared_pkg\n", tmp_path, deps=("declared_pkg",))
    assert issues == []


def test_declared_in_dev_extra_quiet(tmp_path: Path) -> None:
    issues = _scan("import dev_pkg\n", tmp_path, extras={"dev": ("dev_pkg",)})
    assert issues == []


def test_declared_in_any_extra_group_quiet(tmp_path: Path) -> None:
    """Every optional-dependencies group counts, not just the one CI installs."""
    issues = _scan("import telethon\n", tmp_path, extras={"telegram": ("telethon>=1.34",)})
    assert issues == []


def test_declared_with_version_spec_and_extra_quiet(tmp_path: Path) -> None:
    issues = _scan(
        "import uvicorn\nimport my_pkg\n",
        tmp_path,
        deps=("uvicorn[standard]>=0.30", "my_pkg; python_version < '3.13'"),
    )
    assert issues == []


def test_pep503_name_normalization_quiet(tmp_path: Path) -> None:
    """``My-Pkg`` / ``my_pkg`` in pyproject match an ``import my_pkg`` root."""
    issues = _scan("import my_pkg\n", tmp_path, deps=("My-Pkg==2.0",))
    assert issues == []


def test_hyphenated_distribution_name_quiet(tmp_path: Path) -> None:
    """The live repo's sharpest normalization case: ``import eth_utils`` must
    match a declared ``eth-utils`` (distribution names use hyphens, imports
    use underscores)."""
    issues = _scan("import eth_utils\n", tmp_path, deps=("eth-utils>=5.0",))
    assert issues == []


@pytest.mark.parametrize("root", _IMPORT_ROOT_ALIASES)
def test_import_root_alias_quiet_when_distribution_declared(root: str, tmp_path: Path) -> None:
    """An import root whose name differs from its distribution (``sklearn``
    vs ``scikit-learn``) passes only when the distribution is declared."""
    distribution = _IMPORT_ROOT_ALIASES[root]
    issues = _scan(f"import {root}\n", tmp_path, deps=(distribution,))
    assert issues == []


@pytest.mark.parametrize("root", _IMPORT_ROOT_ALIASES)
def test_import_root_alias_flagged_without_declaration(root: str, tmp_path: Path) -> None:
    """The alias never exempts a root by itself — no declaration, still
    flagged (so a dead/renamed alias entry can't silently stop mattering)."""
    issues = _scan(f"import {root}\n", tmp_path)
    assert len(issues) == 1
    assert f"import {root} —" in issues[0]


# ---------------------------------------------------------------------------
# Undeclared third-party roots are flagged
# ---------------------------------------------------------------------------


def test_undeclared_static_flagged(tmp_path: Path) -> None:
    issues = _scan("import mystery_pkg\n", tmp_path)
    assert len(issues) == 1
    assert "import mystery_pkg — not declared in pyproject.toml" in issues[0]


def test_undeclared_from_import_flagged(tmp_path: Path) -> None:
    issues = _scan("from mystery_pkg import thing\n", tmp_path)
    assert len(issues) == 1


def test_undeclared_dotted_import_root_flagged(tmp_path: Path) -> None:
    """The root is the first dotted component, so `import mystery_pkg.thin` is
    flagged under `mystery_pkg` — never a partial/child name."""
    issues = _scan("import mystery_pkg.submodule\n", tmp_path)
    assert len(issues) == 1
    assert "import mystery_pkg" in issues[0]
    assert "mystery_pkg.submodule" not in issues[0]


def test_undeclared_dynamic_import_flagged(tmp_path: Path) -> None:
    issues = _scan('importlib.import_module("mystery_pkg")\n', tmp_path)
    assert len(issues) == 1
    assert 'importlib.import_module("mystery_pkg")' not in issues[0]  # root form
    assert "import mystery_pkg —" in issues[0]


def test_undeclared_dynamic_kwarg_name_flagged(tmp_path: Path) -> None:
    issues = _scan('__import__(name="mystery_pkg")\n', tmp_path)
    assert len(issues) == 1


def test_dynamic_declared_quiet(tmp_path: Path) -> None:
    issues = _scan('importlib.import_module("declared_pkg")\n', tmp_path, deps=("declared_pkg",))
    assert issues == []


# ---------------------------------------------------------------------------
# Always skipped: stdlib, repo-local, relative
# ---------------------------------------------------------------------------


def test_stdlib_quiet(tmp_path: Path) -> None:
    issues = _scan(
        "import os\nimport sys\nimport time\nfrom __future__ import annotations\n",
        tmp_path,
    )
    assert issues == []


def test_repo_local_quiet(tmp_path: Path) -> None:
    issues = _scan("import storage.models\nfrom common.enums import AlertState\n", tmp_path)
    assert issues == []


def test_relative_import_quiet(tmp_path: Path) -> None:
    issues = _scan("from . import storage\n", tmp_path)
    assert issues == []


def test_variable_dynamic_arg_quiet(tmp_path: Path) -> None:
    issues = _scan('name = "mystery_pkg"\nimportlib.import_module(name)\n', tmp_path)
    assert issues == []


# ---------------------------------------------------------------------------
# Registry parsing unit pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("declared_pkg", "declared-pkg"),
        ("My-Pkg==2.0", "my-pkg"),
        ("uvicorn[standard]>=0.30", "uvicorn"),
        ("my_pkg; python_version < '3.13'", "my-pkg"),
        ("psycopg[binary,pool]>=3.2", "psycopg"),
        ("", None),
        ("__bad__", None),
    ],
)
def test_requirement_base_name(requirement: str, expected: str | None) -> None:
    assert _requirement_base_name(requirement) == expected


# ---------------------------------------------------------------------------
# Guard-the-guard: KNOWN_VENV_ABSENT must keep mattering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root", KNOWN_VENV_ABSENT)
def test_known_absent_registry_entry_quiet(root: str, tmp_path: Path) -> None:
    """Registered intentional absences pass even with an empty pyproject —
    the registry consult is alive for the declaration lint, not only the
    collection guard."""
    issues = _scan(f"import {root}\n", tmp_path)
    assert issues == []


@pytest.mark.parametrize("root", KNOWN_VENV_ABSENT)
def test_known_absent_registry_entry_flagged_if_exemption_removed(
    root: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every KNOWN_VENV_ABSENT entry is a REAL exemption: with its row removed
    from the registry, the identical import is flagged as undeclared. A future
    dead row (something now declared in pyproject, or repo-local) is silently
    irrelevant today and is caught here the moment someone adds it."""
    exempted = tuple(r for r in KNOWN_VENV_ABSENT if r != root)
    monkeypatch.setattr("scripts.check_broken_imports.KNOWN_VENV_ABSENT", exempted)
    issues = _scan(f"import {root}\n", tmp_path)
    assert len(issues) == 1
    assert f"import {root} — not declared in pyproject.toml" in issues[0]


# ---------------------------------------------------------------------------
# CLI contract: the real script via subprocess
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the REAL script as a subprocess from the repo root (stdlib-only,
    so no PYTHONPATH setup is needed). Exercises the argparse ``--path`` /
    ``--pyproject`` contract end-to-end — exit code and stdout."""
    return subprocess.run(
        [
            sys.executable,
            str(_repo_root() / "scripts" / "check_declared_deps.py"),
            *args,
        ],
        capture_output=True,
        text=True,
        cwd=_repo_root(),
        timeout=60,
    )


def test_cli_path_flags_only_undeclared_root_in_mixed_fixture(tmp_path: Path) -> None:
    """One fixture holds all four flavors; only the undeclared root is
    flagged, with its path:line prefix, while the declared, stdlib, and
    repo-local imports stay absent from the output."""
    repo = _make_repo(tmp_path, deps=("declared_pkg",))
    target = repo / "mixed_fixture.py"
    target.write_text(
        "import declared_pkg\nimport mystery_pkg\nimport os\nimport storage.models\n",
        encoding="utf-8",
    )
    proc = _run_cli("--path", str(target), "--pyproject", str(repo / "pyproject.toml"))
    assert proc.returncode == 1
    assert proc.stderr == ""
    out = proc.stdout
    assert "declared-deps scan FAILED" in out
    assert f"{target}:2: import mystery_pkg — not declared in pyproject.toml" in out
    assert "import declared_pkg" not in out
    assert "import os" not in out
    assert "import storage.models" not in out


def test_cli_path_clean_fixture_exits_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, deps=("declared_pkg",))
    target = repo / "clean_fixture.py"
    target.write_text("import declared_pkg\nimport os\nimport storage.models\n", encoding="utf-8")
    proc = _run_cli("--path", str(target), "--pyproject", str(repo / "pyproject.toml"))
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "declared-deps scan OK — 1 third-party root(s)" in proc.stdout


def test_cli_missing_pyproject_fails(tmp_path: Path) -> None:
    """A missing pyproject must fail loudly, not scan against an empty
    declared set (which would misattribute every failure)."""
    target = tmp_path / "wip.py"
    target.write_text("import anything\n", encoding="utf-8")
    proc = _run_cli("--path", str(target), "--pyproject", str(tmp_path / "nope.toml"))
    assert proc.returncode == 1
    assert "pyproject.toml not found" in proc.stdout


def test_cli_path_repo_relative_self_scan_exits_zero() -> None:
    """The real file scanned against the REAL pyproject: its imports are
    stdlib, repo-local, or declared, so the live contract is self-pinned."""
    proc = _run_cli("--path", "tests/test_check_declared_deps.py")
    assert proc.returncode == 0
    assert "declared-deps scan OK" in proc.stdout
