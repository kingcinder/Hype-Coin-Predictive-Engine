"""CI dependency-declaration guard: every third-party import root the repo
uses must be declared in ``pyproject.toml``.

History: a manual audit of the repo's third-party import roots found the
tree healthy — every root mapped to a declared dependency, a ``[dev]`` /
optional extra, a transitive install (``fastapi`` -> ``pydantic``,
``uvicorn[standard]`` -> ``websockets``), or ``KNOWN_VENV_ABSENT``. But that
audit was a one-off hand check: a package pulled in manually
(``pip install foo``) or transitively would silently drift back in, resolve
on a developer machine, and only break a fresh ``pip install -e ".[dev]"``
later (in the Tests job, or worse, production). This script automates the
audit so the drift is impossible to re-introduce silently.

What it does
------------
1. Walks every ``.py`` file under the repo and collects the set of
   third-party import *roots* — the first dotted component of each static
   import (``import X``, ``from X import y``) and of each dynamic load whose
   module name is a string literal (``importlib.import_module("X")`` /
   ``__import__("X")``), the exact same dynamic recognition the collection
   guard uses.
2. Skips ``stdlib`` roots (``sys.stdlib_module_names`` — frozen modules with
   no file on disk included) and repo-local roots (top-level dirs/files of
   this checkout, which are declared by their existence).
3. Fails for every remaining root unless its PEP 503-normalized name is
   declared in ``[project.dependencies]`` or any ``[project.optional-
   dependencies]`` group (``dev``, ``telegram``, ... — every declared group
   counts, not just the one CI installs), or is listed in
   ``KNOWN_VENV_ABSENT`` (the committed, per-entry-justified registry of
   intentional optional extras, shared with ``check_broken_imports.py`` — a
   registry edit updates both lints, and both are guarded by their test
   suites).

   PEP 503 name normalization bridges the import-name/distribution-name
   split (``import eth_utils`` matches a declared ``eth-utils``), and
   ``_IMPORT_ROOT_ALIASES`` covers genuine renames (``sklearn`` is
   distributed as ``scikit-learn``).

The script is stdlib-only on purpose, like the collection guard: ``tomllib``
parses ``pyproject.toml`` (requires-python is >=3.12), and nothing but the
standard library is imported, so the plain scan runs on a bare checkout.

Usage:
    python scripts/check_declared_deps.py                    # scan the whole repo
    python scripts/check_declared_deps.py --path ui/         # scan specific paths
    python scripts/check_declared_deps.py --pyproject other/pyproject.toml
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

# Allow direct execution (``python scripts/check_declared_deps.py``) to import
# the shared registry and collectors from the sibling guard. Under pytest the
# repo root is already on sys.path, so this is a no-op there.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import check_broken_imports  # noqa: E402
from scripts.check_broken_imports import (  # noqa: E402
    _STDLIB_TOP_LEVEL,
    _dynamic_import_module,
    _iter_py_files,
)


def _requirement_base_name(requirement: str) -> str | None:
    """PEP 503-normalized distribution name from one requirement string.

    Handles env markers (``; python_version < "3.12"`` — drop everything
    from the ``;``), extras (``uvicorn[standard]`` — drop the ``[...]``),
    and the version specifier that follows the name. Returns None when no
    name token is present, or normalizes (lowercases, maps runs of ``._-``
    to ``-``) so ``My-Pkg`` / ``my_pkg`` / ``my.pkg`` all match an
    ``import my_pkg`` root — imports are lowercase, pyproject names aren't.
    """
    base = requirement.split(";", 1)[0].split("[", 1)[0].strip()
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", base)
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


# Import roots whose name differs from their distribution name in
# pyproject.toml. "sklearn" is the canonical case: the project ships
# ``import sklearn`` but the PyPI distribution is ``scikit-learn``. Each
# entry maps an import root to the declared distribution name it satisfies.
_IMPORT_ROOT_ALIASES: dict[str, str] = {
    "sklearn": "scikit-learn",
}


def _declared_package_names(pyproject: Path) -> frozenset[str]:
    """Normalized names declared in ``[project.dependencies]`` and every
    ``[project.optional-dependencies]`` group of ``pyproject``."""
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project", {})
    names: set[str] = set()
    for requirement in project.get("dependencies", []) or []:
        name = _requirement_base_name(str(requirement))
        if name is not None:
            names.add(name)
    for requirements in (project.get("optional-dependencies", {}) or {}).values():
        for requirement in requirements or []:
            name = _requirement_base_name(str(requirement))
            if name is not None:
                names.add(name)
    return frozenset(names)


def _collect_roots(files: list[Path], repo: Path) -> dict[str, list[tuple[Path, int]]]:
    """Third-party import roots used by ``files``, with evidence.

    The root of each import is the first dotted component; stdlib and
    repo-local (top-level file/dir under ``repo``) roots are skipped. The
    evidence list lets the report show the first occurrence plus a count.
    """
    roots: dict[str, list[tuple[Path, int]]] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue  # the collection guard reports parse failures
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative — resolves within its own package
                if node.module:
                    modules = [node.module]
            elif isinstance(node, ast.Call):
                dynamic = _dynamic_import_module(node)
                if dynamic is not None:
                    modules = [str(dynamic[1].value)]
            for module in modules:
                root = module.split(".")[0]
                if root in _STDLIB_TOP_LEVEL or (repo / root).exists():
                    continue
                roots.setdefault(root, []).append((path, node.lineno))
    return roots


def _undeclared_issues(
    roots: dict[str, list[tuple[Path, int]]],
    declared: frozenset[str],
) -> list[str]:
    issues: list[str] = []
    for root in sorted(roots):
        canonical = _requirement_base_name(_IMPORT_ROOT_ALIASES.get(root, root))
        if (canonical is not None and canonical in declared) or (
            root in check_broken_imports.KNOWN_VENV_ABSENT
        ):
            continue
        path, lineno = roots[root][0]
        count = len(roots[root])
        more = f" (+{count - 1} more)" if count > 1 else ""
        issues.append(
            f"{path}:{lineno}: import {root} — not declared in pyproject.toml; "
            f"add it to [project.dependencies]/optional-dependencies or "
            f"KNOWN_VENV_ABSENT{more}"
        )
    return issues


def scan_files(files: list[Path], repo: Path, *, pyproject: Path) -> list[str]:
    """Return undeclared-third-party-root issues for ``files``, or an empty list."""
    return _undeclared_issues(_collect_roots(files, repo), _declared_package_names(pyproject))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail on third-party imports not declared in pyproject.toml"
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="PATH",
        help="scan only this file/dir (repeatable); default: the whole repo",
    )
    parser.add_argument(
        "--pyproject",
        metavar="PATH",
        default=None,
        help="pyproject.toml to check against (default: <repo>/pyproject.toml)",
    )
    args = parser.parse_args(argv)

    repo = _REPO_ROOT
    pyproject = Path(args.pyproject) if args.pyproject else repo / "pyproject.toml"
    if not pyproject.is_absolute():
        pyproject = repo / pyproject  # resolve like --path does
    if not pyproject.is_file():
        print(f"declared-deps scan FAILED — pyproject.toml not found at {pyproject}")
        return 1

    files: list[Path] = []
    for target in args.path or ["."]:
        p = Path(target)
        if not p.is_absolute():
            p = repo / p
        if p.is_dir():
            files.extend(_iter_py_files(p))
        elif p.is_file() and p.suffix == ".py":
            files.append(p)
    files = sorted(set(files))

    roots = _collect_roots(files, repo)
    issues = _undeclared_issues(roots, _declared_package_names(pyproject))
    if not issues:
        print(
            f"declared-deps scan OK — {len(roots)} third-party root(s), "
            f"{len(files)} .py file(s) checked"
        )
        return 0
    print(
        "declared-deps scan FAILED — "
        f"{len(issues)} third-party import root(s) not declared in pyproject.toml:\n"
    )
    for issue in issues:
        print(f"  {issue}")
    print(
        "\nDeclare each root in pyproject.toml ([project.dependencies] or an "
        "optional-dependencies group)\nor add it to KNOWN_VENV_ABSENT in "
        "scripts/check_broken_imports.py with a why-comment."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
