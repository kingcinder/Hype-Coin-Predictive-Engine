"""Unit tests for the broken-import collection guard
(scripts/check_broken_imports.py).

Pins the AST-based scanner: static imports, dynamic loads
(``importlib.import_module`` / ``__import__`` with string-literal module
names), and the deliberate no-flag cases (third-party roots, relative
imports, variable/expression name arguments). Snippets are plain strings
written to temp files and parsed by ``scan_files`` against a small synthetic
repo tree (``storage/models.py`` + ``common/enums.py``) — hermetic, with no
dependency on the live repo layout. The test module itself never contains a
live broken import, so the whole-repo scan stays clean.

Beyond the ``scan_files`` API, a CLI section runs the REAL script as a
subprocess (``python scripts/check_broken_imports.py --path ...``) against
tmp fixture files, pinning exit codes (0 clean / 1 broken), message content,
and the ``--path`` branches (absolute file, repo-relative file, directory).

A final guard-the-guard section parametrizes over the script's own
registries: every ``KNOWN_BROKEN_IMPORTS`` entry must provably trip
``scan_files`` — via the registry's own message branch, for its exact name
AND every submodule prefix underneath it (the registry denylists whole
subtrees) — and every ``KNOWN_VENV_ABSENT`` entry must be a REAL exclusion,
so a registry row can never silently stop mattering.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_broken_imports import (
    _DYNAMIC_IMPORT_FORMS,
    KNOWN_BROKEN_IMPORTS,
    KNOWN_VENV_ABSENT,
    _repo_root,
    _venv_site_packages,
    scan_files,
)

# Namespace-package layout the resolver can walk (no __init__.py required).
# Only these two modules exist; every other reference either resolves against
# them or fails as a dangling path — mirroring the real repo's behavior
# without coupling the tests to its live file tree.
_SYNTHETIC_MODULES = ("storage/models.py", "common/enums.py")


def _synthetic_repo(tmp_path: Path) -> Path:
    """Hermetic repo root: provides ``_SYNTHETIC_MODULES``, nothing else."""
    repo = tmp_path / "repo"
    for rel in _SYNTHETIC_MODULES:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return repo


def _scan(
    snippet: str,
    tmp_path: Path,
    *,
    filename: str = "mod.py",
    venv_root: Path | None = None,
) -> list[str]:
    """Scan exactly one temp snippet against a hermetic synthetic repo."""
    repo = _synthetic_repo(tmp_path)
    site_packages = _venv_site_packages(venv_root) if venv_root is not None else None
    target = repo / filename
    target.write_text(snippet, encoding="utf-8")
    return scan_files([target], repo, site_packages=site_packages)


def _synthetic_venv(tmp_path: Path) -> Path:
    """Hermetic venv layout: ``fake_pkg`` (package dir) and ``single_mod``
    (bare module) are installed; every other third-party name is missing."""
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    (site / "fake_pkg").mkdir(parents=True)
    (site / "single_mod.py").write_text("", encoding="utf-8")
    return tmp_path / "venv"


# ---------------------------------------------------------------------------
# Static imports (regression: the original failure class)
# ---------------------------------------------------------------------------


def test_static_known_broken_import_flagged(tmp_path: Path) -> None:
    issues = _scan("import storage.schema\n", tmp_path)
    assert len(issues) == 1
    assert "known-broken import" in issues[0]


def test_static_known_broken_from_flagged(tmp_path: Path) -> None:
    issues = _scan("from storage.schema import metadata\n", tmp_path)
    assert len(issues) == 1
    assert "import storage.schema —" in issues[0]
    assert "known-broken import" in issues[0]


def test_static_resolver_miss_flagged(tmp_path: Path) -> None:
    issues = _scan("from storage.no_such import x\n", tmp_path)
    assert len(issues) == 1
    assert "would break pytest collection" in issues[0]


# ---------------------------------------------------------------------------
# Dynamic loads: string-literal module names ARE checked
# ---------------------------------------------------------------------------


def test_dynamic_import_module_known_broken_flagged(tmp_path: Path) -> None:
    issues = _scan('import importlib\nimportlib.import_module("storage.schema")\n', tmp_path)
    assert len(issues) == 1
    assert 'importlib.import_module("storage.schema")' in issues[0]
    assert "known-broken import" in issues[0]


def test_dynamic_kwarg_name_flagged(tmp_path: Path) -> None:
    issues = _scan('import importlib\nimportlib.import_module(name="storage.schema")\n', tmp_path)
    assert len(issues) == 1
    assert 'importlib.import_module("storage.schema")' in issues[0]


def test___import___known_broken_flagged(tmp_path: Path) -> None:
    issues = _scan('__import__("storage.schema")\n', tmp_path)
    assert len(issues) == 1
    assert '__import__("storage.schema")' in issues[0]


def test___import___kwarg_name_flagged(tmp_path: Path) -> None:
    issues = _scan('__import__(name="storage.schema")\n', tmp_path)
    assert len(issues) == 1
    assert '__import__("storage.schema")' in issues[0]


def test_dynamic_resolver_miss_flagged(tmp_path: Path) -> None:
    issues = _scan(
        'import importlib\nimportlib.import_module("storage.no_such_module")\n', tmp_path
    )
    assert len(issues) == 1
    assert "would not resolve at runtime" in issues[0]


def test_valid_repo_local_dynamic_not_flagged(tmp_path: Path) -> None:
    issues = _scan('import importlib\nimportlib.import_module("storage.models")\n', tmp_path)
    assert issues == []


def test_static_and_dynamic_both_flagged(tmp_path: Path) -> None:
    issues = _scan('import storage.schema\nimportlib.import_module("storage.schema")\n', tmp_path)
    assert len(issues) == 2


# Resolver-style dynamic forms: importlib.util.find_spec, the long-gone
# importlib.find_loader, and the pkgutil family (resolve_name + the legacy
# loader lookups) — swept exactly like import_module / __import__, and
# parametrized over the script's own form table so a form added there is
# covered automatically.
_RESOLVER_DESCRIPTORS = tuple(d for d, _ in _DYNAMIC_IMPORT_FORMS)


@pytest.mark.parametrize("descriptor", _RESOLVER_DESCRIPTORS)
def test_every_dynamic_form_known_broken_flagged(descriptor: str, tmp_path: Path) -> None:
    """Every registered resolver form flags a known-broken module name with
    the registry's phrase — find_spec / pkgutil included."""
    issues = _scan(f'{descriptor}("storage.schema")\n', tmp_path)
    assert len(issues) == 1
    assert f'{descriptor}("storage.schema")' in issues[0]
    assert "known-broken import" in issues[0]


def test_find_spec_kwarg_name_flagged(tmp_path: Path) -> None:
    """``find_spec``'s first parameter is literally named ``name``, so the
    keyword form is swept like ``import_module``'s."""
    issues = _scan('importlib.util.find_spec(name="storage.schema")\n', tmp_path)
    assert len(issues) == 1
    assert 'importlib.util.find_spec("storage.schema")' in issues[0]


@pytest.mark.parametrize("descriptor", _RESOLVER_DESCRIPTORS)
def test_every_dynamic_form_resolver_miss_flagged(descriptor: str, tmp_path: Path) -> None:
    """A dangling repo-local path passed to any resolver form is a runtime
    miss, not a known-broken hit. The path is deliberately deep
    (``storage.no_such.sub``): ``pkgutil.resolve_name`` attr-exempts only its
    FINAL segment, so the dangling segment must sit before the tail to stay
    flaggable under that form too."""
    issues = _scan(f'{descriptor}("storage.no_such.sub")\n', tmp_path)
    assert len(issues) == 1
    assert "would not resolve at runtime" in issues[0]


@pytest.mark.parametrize("descriptor", _RESOLVER_DESCRIPTORS)
def test_every_dynamic_form_valid_repo_local_quiet(descriptor: str, tmp_path: Path) -> None:
    issues = _scan(f'{descriptor}("storage.models")\n', tmp_path)
    assert issues == []


# ---------------------------------------------------------------------------
# Dynamic loads that must NOT be flagged
# ---------------------------------------------------------------------------


def test_third_party_dynamic_not_flagged(tmp_path: Path) -> None:
    issues = _scan(
        "import importlib\n"
        'importlib.import_module("sqlalchemy")\n'
        'importlib.import_module("os")\n'
        '__import__("numpy")\n',
        tmp_path,
    )
    assert issues == []


def test_variable_arg_not_flagged(tmp_path: Path) -> None:
    issues = _scan(
        'import importlib\nname = "storage.schema"\nimportlib.import_module(name)\n',
        tmp_path,
    )
    assert issues == []


def test_expression_arg_not_flagged(tmp_path: Path) -> None:
    issues = _scan(
        'import importlib\nPREFIX = "storage"\nimportlib.import_module(PREFIX + ".schema")\n',
        tmp_path,
    )
    assert issues == []


def test_bare_import_module_name_not_flagged(tmp_path: Path) -> None:
    """A bare ``import_module`` call could be a local shadow (e.g. after
    ``from importlib import import_module`` or a wildcard import), so only
    the canonical ``importlib.import_module`` attribute form is matched."""
    issues = _scan(
        'from importlib import import_module\nimport_module("storage.schema")\n',
        tmp_path,
    )
    assert issues == []


def test_relative_import_not_flagged(tmp_path: Path) -> None:
    issues = _scan("from . import storage\n", tmp_path)
    assert issues == []


@pytest.mark.parametrize("descriptor", _RESOLVER_DESCRIPTORS)
def test_every_dynamic_form_variable_arg_quiet(descriptor: str, tmp_path: Path) -> None:
    issues = _scan(f'mod = "storage.schema"\n{descriptor}(mod)\n', tmp_path)
    assert issues == []


@pytest.mark.parametrize("descriptor", _RESOLVER_DESCRIPTORS)
def test_every_dynamic_form_expression_arg_quiet(descriptor: str, tmp_path: Path) -> None:
    issues = _scan(f'PREFIX = "storage"\n{descriptor}(PREFIX + ".schema")\n', tmp_path)
    assert issues == []


def test_bare_resolver_name_not_flagged(tmp_path: Path) -> None:
    """A bare ``find_spec`` / ``resolve_name`` / ``find_loader`` name could
    be a local shadow or wildcard import — only canonical module paths are
    matched."""
    issues = _scan(
        'find_spec("storage.schema")\n'
        'resolve_name("storage.schema")\n'
        'find_loader("storage.schema")\n',
        tmp_path,
    )
    assert issues == []


def test_reexported_util_alias_not_flagged(tmp_path: Path) -> None:
    """``from importlib import util`` then ``util.find_spec(...)``: the bare
    alias's binding provenance is unknown, so it is left unverified like a
    bare name."""
    issues = _scan(
        'from importlib import util\nutil.find_spec("storage.schema")\n',
        tmp_path,
    )
    assert issues == []


def test_rebound_module_alias_not_flagged(tmp_path: Path) -> None:
    issues = _scan(
        'import pkgutil as p\np.resolve_name("storage.schema")\n',
        tmp_path,
    )
    assert issues == []


def test_resolve_name_module_attr_tail_quiet(tmp_path: Path) -> None:
    """``pkgutil.resolve_name("module.attr")`` is the canonical usage: the
    final segment may be an attribute on a resolvable module, so when the
    module prefix resolves the tail is left unverified instead of flagged."""
    issues = _scan('pkgutil.resolve_name("storage.models.SomeClass")\n', tmp_path)
    assert issues == []


def test_resolve_name_known_broken_still_flagged(tmp_path: Path) -> None:
    """The attr-tail softening never trumps the registry: a denylisted name
    — or anything under one — flags even through the attr interpretation."""
    issues = _scan(
        'pkgutil.resolve_name("storage.schema.attr")\npkgutil.resolve_name("storage.schema")\n',
        tmp_path,
    )
    assert len(issues) == 2
    assert all("known-broken import" in issue for issue in issues)


def test_resolve_name_dangling_module_still_flagged(tmp_path: Path) -> None:
    """A genuinely dangling module prefix keeps flagging: no attr reading can
    save ``storage.no_such`` — the stripped probe ``storage.no_such`` fails
    too — so the full reference stays a runtime miss."""
    issues = _scan('pkgutil.resolve_name("storage.no_such.Thing")\n', tmp_path)
    assert len(issues) == 1
    assert "would not resolve at runtime" in issues[0]


def test_resolve_name_two_segment_attr_boundary_quiet(tmp_path: Path) -> None:
    """The two-segment ``module.attr`` shape is attr-exempt even when the
    attribute is unknown: ``storage.no_such`` reads as module ``storage`` +
    attribute ``no_such``, which is statically unverifiable, so it stays
    quiet — only a FAILING module prefix is flaggable."""
    issues = _scan('pkgutil.resolve_name("storage.no_such")\n', tmp_path)
    assert issues == []


# ---------------------------------------------------------------------------
# Valid repo-local references stay quiet
# ---------------------------------------------------------------------------


def test_valid_repo_local_static_not_flagged(tmp_path: Path) -> None:
    issues = _scan("import storage.models\nfrom common.enums import AlertState\n", tmp_path)
    assert issues == []


# ---------------------------------------------------------------------------
# CLI contract: the real script run via `--path` (subprocess)
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the REAL script as a subprocess from the repo root.

    The script is stdlib-only, so no PYTHONPATH setup is needed. This
    exercises the argparse ``--path`` contract end-to-end — exit code and
    stdout — rather than calling ``scan_files`` directly.
    """
    return subprocess.run(
        [
            sys.executable,
            str(_repo_root() / "scripts" / "check_broken_imports.py"),
            *args,
        ],
        capture_output=True,
        text=True,
        cwd=_repo_root(),
        timeout=60,
    )


def test_cli_path_flags_only_broken_import_in_mixed_fixture(tmp_path: Path) -> None:
    """One fixture holds all three flavors: the single broken reference is
    flagged with exit code 1, while the legit repo import and the third-party
    import stay absent from the output."""
    target = tmp_path / "mixed_fixture.py"
    target.write_text(
        "import storage.schema\n"
        "import storage.models\n"
        "import sqlalchemy\n"
        "import importlib\n"
        'importlib.import_module("storage.schema")\n',
        encoding="utf-8",
    )
    proc = _run_cli("--path", str(target))
    assert proc.returncode == 1
    assert proc.stderr == ""
    out = proc.stdout
    assert "broken-import scan FAILED" in out
    assert f"{target}:1: import storage.schema — known-broken import" in out
    assert 'importlib.import_module("storage.schema")' in out
    assert "sqlalchemy" not in out
    # The failure hint prints "storage/models.py" with a slash — the dotted
    # form below can only come from a flagged issue line. Keep in sync if the
    # hint paragraph in main() is ever reworded.
    assert "storage.models" not in out


def test_cli_path_clean_fixture_exits_zero(tmp_path: Path) -> None:
    """Legit repo import + third-party imports, nothing broken -> exit 0."""
    target = tmp_path / "clean_fixture.py"
    target.write_text(
        "import storage.models\n"
        "from common.enums import AlertState\n"
        "import sqlalchemy\n"
        "import os\n",
        encoding="utf-8",
    )
    proc = _run_cli("--path", str(target))
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "broken-import scan OK — 1 .py file(s) checked" in proc.stdout


def test_cli_path_repo_relative_clean_file_exits_zero() -> None:
    """The repo-relative branch of ``--path`` against a real in-repo file."""
    proc = _run_cli("--path", "tests/test_check_broken_imports.py")
    assert proc.returncode == 0
    assert "broken-import scan OK — 1 .py file(s) checked" in proc.stdout


def test_cli_path_directory_flags_only_bad_file(tmp_path: Path) -> None:
    """``--path`` on a directory scans every .py under it; only the broken
    file is flagged, and its message names that file's path:line."""
    fixture_dir = tmp_path / "scandir"
    fixture_dir.mkdir()
    (fixture_dir / "bad.py").write_text("from storage.schema import metadata\n", encoding="utf-8")
    (fixture_dir / "ok.py").write_text("import storage.models\n", encoding="utf-8")
    proc = _run_cli("--path", str(fixture_dir))
    assert proc.returncode == 1
    out = proc.stdout
    assert f"{fixture_dir / 'bad.py'}:1: import storage.schema" in out
    # See test_cli_path_flags_only_broken_import_in_mixed_fixture: the hint
    # paragraph spells it "storage/models.py", so the dotted form can only
    # come from a flagged issue line.
    assert "storage.models" not in out


# ---------------------------------------------------------------------------
# --venv: third-party roots resolve against the venv (stdlib + site-packages)
# ---------------------------------------------------------------------------


def test_no_venv_unknown_root_still_skipped(tmp_path: Path) -> None:
    """Without ``--venv`` an unknown root stays an unverified third-party
    import — the backward-compatible default."""
    issues = _scan("import not_installed_pkg\n", tmp_path)
    assert issues == []


def test_venv_installed_package_quiet(tmp_path: Path) -> None:
    issues = _scan(
        "import fake_pkg\nfrom fake_pkg import thing\n",
        tmp_path,
        venv_root=_synthetic_venv(tmp_path),
    )
    assert issues == []


def test_venv_installed_single_module_quiet(tmp_path: Path) -> None:
    issues = _scan("import single_mod\n", tmp_path, venv_root=_synthetic_venv(tmp_path))
    assert issues == []


def test_venv_stdlib_import_quiet(tmp_path: Path) -> None:
    """Stdlib — including frozen modules with no file on disk (sys, time) —
    is never a venv miss: ``sys.stdlib_module_names`` governs, not
    site-packages."""
    issues = _scan(
        "import os\nimport sys\nimport time\nfrom __future__ import annotations\n",
        tmp_path,
        venv_root=_synthetic_venv(tmp_path),
    )
    assert issues == []


def test_venv_missing_package_static_flagged(tmp_path: Path) -> None:
    issues = _scan("import not_installed_pkg\n", tmp_path, venv_root=_synthetic_venv(tmp_path))
    assert len(issues) == 1
    assert "not found in repo or venv site-packages" in issues[0]
    assert "would break pytest collection" in issues[0]


@pytest.mark.parametrize("root", KNOWN_VENV_ABSENT)
def test_venv_registered_absent_optional_quiet(root: str, tmp_path: Path) -> None:
    """Every root in KNOWN_VENV_ABSENT (intentional optional extras the lint
    venv doesn't install, e.g. telethon) never trips the --venv check —
    parametrized over the registry so a future entry is covered or the test
    fails for an entry that stops being allowed."""
    issues = _scan(f"import {root}\n", tmp_path, venv_root=_synthetic_venv(tmp_path))
    assert issues == []


def test_venv_missing_package_from_import_flagged(tmp_path: Path) -> None:
    issues = _scan(
        "from not_installed_pkg import thing\n",
        tmp_path,
        venv_root=_synthetic_venv(tmp_path),
    )
    assert len(issues) == 1


def test_venv_missing_package_dynamic_flagged(tmp_path: Path) -> None:
    issues = _scan(
        'import importlib\nimportlib.import_module("not_installed_pkg")\n',
        tmp_path,
        venv_root=_synthetic_venv(tmp_path),
    )
    assert len(issues) == 1
    assert 'importlib.import_module("not_installed_pkg")' in issues[0]
    assert "module would not resolve at runtime" in issues[0]


def test_cli_venv_flag_flags_missing_package(tmp_path: Path) -> None:
    target = tmp_path / "wip.py"
    target.write_text("import not_installed_pkg\nimport fake_pkg\nimport os\n", encoding="utf-8")
    proc = _run_cli("--venv", str(_synthetic_venv(tmp_path)), "--path", str(target))
    assert proc.returncode == 1
    out = proc.stdout
    assert f"{target}:1: import not_installed_pkg" in out
    assert "not found in repo or venv site-packages" in out
    assert "import fake_pkg" not in out
    assert "import os" not in out


def test_cli_without_venv_skips_unknown_third_party(tmp_path: Path) -> None:
    target = tmp_path / "wip.py"
    target.write_text("import not_installed_pkg\n", encoding="utf-8")
    proc = _run_cli("--path", str(target))
    assert proc.returncode == 0
    assert "broken-import scan OK" in proc.stdout


def test_cli_venv_missing_site_packages_warns_but_passes(tmp_path: Path) -> None:
    """A ``--venv`` pointing at a non-venv degrades gracefully: a stderr
    warning, third-party left unverified, and exit code 0 — an optional flag
    can never break the lint gate."""
    target = tmp_path / "wip.py"
    target.write_text("import not_installed_pkg\n", encoding="utf-8")
    not_a_venv = tmp_path / "not_a_venv"
    not_a_venv.mkdir()
    proc = _run_cli("--venv", str(not_a_venv), "--path", str(target))
    assert proc.returncode == 0
    assert "no site-packages found" in proc.stderr
    assert "broken-import scan OK" in proc.stdout


# ---------------------------------------------------------------------------
# Guard-the-guard: registry entries must keep mattering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root", KNOWN_BROKEN_IMPORTS)
def test_known_broken_registry_entry_trips_static_scan(root: str, tmp_path: Path) -> None:
    """Every KNOWN_BROKEN_IMPORTS entry provably trips ``scan_files`` on a
    static import — and specifically via the registry's own branch: the
    "known-broken import" phrase exists nowhere else in the output, so this
    reddens if the registry (or its loop in ``_module_error``) is ever
    refactored into silence. The module-side prefix is asserted (rather than
    the matched row's name) so a future nested entry whose parent shadows it
    via the prefix match still passes.
    """
    issues = _scan(f"import {root}\n", tmp_path)
    assert len(issues) == 1
    assert f"import {root} — known-broken import" in issues[0]


@pytest.mark.parametrize("root", KNOWN_BROKEN_IMPORTS)
def test_known_broken_registry_entry_trips_dynamic_scan(root: str, tmp_path: Path) -> None:
    """The same registry entry also trips the dynamic-load half, pinning the
    shared ``_module_error`` consult from both callers so neither the static
    nor the dynamic path can silently stop consulting the registry.
    """
    issues = _scan(
        f'import importlib\nimportlib.import_module("{root}")\n',
        tmp_path,
    )
    assert len(issues) == 1
    assert "known-broken import" in issues[0]
    assert "module would not resolve at runtime" not in issues[0]


@pytest.mark.parametrize("root", KNOWN_BROKEN_IMPORTS)
def test_known_broken_registry_prefix_branch_trips_static(root: str, tmp_path: Path) -> None:
    """Importing a submodule under each registered prefix must ALSO trip the
    registry's prefix branch (``module.startswith(f"{broken}.")``) — the
    registry denylists whole subtrees (its second documented purpose is
    denylisting a real-but-deprecated module the resolver would pass). A
    refactor to exact-match-only would silently un-flag submodule imports;
    the registry-only "known-broken import" phrase would vanish and this
    would redden. Module-side assertion, so a future nested entry whose
    parent shadows it via the prefix match still passes.
    """
    issues = _scan(f"import {root}.submodule\n", tmp_path)
    assert len(issues) == 1
    assert f"import {root}.submodule — known-broken import" in issues[0]


@pytest.mark.parametrize("root", KNOWN_BROKEN_IMPORTS)
def test_known_broken_registry_prefix_branch_trips_from_import(root: str, tmp_path: Path) -> None:
    """The from-import form of a submodule reference hits the same prefix
    branch — and is reported under the module path, never a resolver miss.
    """
    issues = _scan(f"from {root}.submodule import metadata\n", tmp_path)
    assert len(issues) == 1
    assert f"import {root}.submodule — known-broken import" in issues[0]
    assert "would break pytest collection" not in issues[0]


@pytest.mark.parametrize("root", KNOWN_BROKEN_IMPORTS)
def test_known_broken_registry_prefix_branch_trips_dynamic(root: str, tmp_path: Path) -> None:
    """The dynamic-load half covers registered subtrees the same way: a
    string-literal ``import_module`` of a module under the prefix is flagged
    by the registry branch and phrased as known-broken, never a runtime miss.
    """
    issues = _scan(
        f'import importlib\nimportlib.import_module("{root}.submodule")\n',
        tmp_path,
    )
    assert len(issues) == 1
    assert f'importlib.import_module("{root}.submodule")' in issues[0]
    assert "known-broken import" in issues[0]
    assert "module would not resolve at runtime" not in issues[0]


@pytest.mark.parametrize("root", KNOWN_VENV_ABSENT)
def test_venv_abs_registry_entry_flagged_without_exemption(
    root: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every KNOWN_VENV_ABSENT entry is a REAL exclusion: with just its own
    exemption removed (the other rows intact), the identical import must fail
    the ``--venv`` check. A future dead row — a package the lint venv
    actually installs, a repo-local name, or a stdlib module — is silently
    irrelevant today, so it reddens here the moment someone adds it. This is
    the flagged side of the contract that
    ``test_venv_registered_absent_optional_quiet`` pins suppressed.
    """
    exempted = tuple(r for r in KNOWN_VENV_ABSENT if r != root)
    monkeypatch.setattr("scripts.check_broken_imports.KNOWN_VENV_ABSENT", exempted)
    issues = _scan(f"import {root}\n", tmp_path, venv_root=_synthetic_venv(tmp_path))
    assert len(issues) == 1
    assert f"`{root}` not found in repo or venv site-packages" in issues[0]
