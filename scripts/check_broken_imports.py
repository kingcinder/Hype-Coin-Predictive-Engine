"""CI collection guard: flag imports that would break ``pytest`` collection.

History: a WIP test once did ``from storage.schema import metadata`` — a module
that does not exist (the schema lives in ``storage/models.py`` +
``storage/schema.sql``). Because test modules import eagerly, that single
dangling import broke collection for the ENTIRE suite and the Tests job went
red with a ``ModuleNotFoundError`` surfaced in an unrelated file. This check
is the guard that makes that failure class impossible to re-introduce
silently.

What it does
------------
1. ``KNOWN_BROKEN_IMPORTS`` — a committed registry of module paths known to
   have broken collection before (e.g. ``storage.schema``). Every import
   statement whose name starts with one of these prefixes is flagged
   verbatim. This is the explicit "grep the imports for known-broken names"
   part; it also allows denylisting a real-but-deprecated module if needed.
2. Dotted-import resolution — every absolute dotted import whose first
   component is a repo-local package (a top-level directory of this repo,
   e.g. ``storage``, ``common``, ``tests``) is resolved against the real file
   tree: each component must map to ``<dir>.py`` or a subdirectory (namespace
   package). ``storage.schema`` fails here too, and so would ANY future
   typo'd or renamed repo-internal import — no registry editing required to
   catch new breakages.

Scope: imports of third-party packages (``sqlalchemy``, ``numpy``,
``pytest``, ...) are skipped by default — their availability is the
dependency-install job's job — but with ``--venv PATH`` every third-party
root must also resolve against that venv's package environment (stdlib
first, then ``site-packages``), so a WIP importing a not-yet-installed
package is caught at lint time instead of only breaking the Tests job,
unless the root is an intentional optional extra listed in
``KNOWN_VENV_ABSENT`` (e.g. the opt-in telegram crawler's ``telethon``).
Relative imports are skipped (they resolve within their own package and
follow literally from the file layout). Dynamic loads are
checked too, but conservatively: ``importlib.import_module(...)``,
``importlib.util.find_spec(...)``, the ``pkgutil`` resolvers (``resolve_name``
and the legacy ``find_loader`` / ``get_loader`` loader lookups), and
``__import__(...)`` are only flagged when the module name is a
string-literal constant — matching ``KNOWN_BROKEN_IMPORTS`` or failing the
same repo-tree resolution as a static import would. Variable or expression
name arguments (``import_module(module_var)``) are deliberately not flagged:
the reference can't be verified statically, so a stale name would be
unverifiable guesswork; nor are constant-foldable expressions such as
``import_module(f"storage.schema")`` or ``import_module("storage" + ".schema")``
— only a literal string constant is statically checkable without evaluation.
One form is special-cased: ``pkgutil.resolve_name`` also accepts
``module.attr`` references, so when its full dotted string fails *resolver*
resolution the sweep verifies just the module prefix and leaves the final
segment (an attribute candidate) unverified — the known-broken registry
still trumps, and a genuinely dangling module prefix still flags.
Checking is AST-based on purpose: raw-text grep would false-positive on
docstrings and comments that merely mention a dead module. (The script's own
docstring names ``storage.schema`` throughout — proof that AST-only is the
right call.)

Stdlib-only on purpose: the script's own logic imports nothing but the
standard library, so the plain scan (no ``--venv``) runs on a bare checkout
and cannot itself be defeated by a broken install step. The ``--venv``
resolution is the one part that leans on an installed environment — in the
CI lint job that's the venv the job explicitly creates before the scan.

Usage:
    python scripts/check_broken_imports.py              # scan the whole repo
    python scripts/check_broken_imports.py --path ui/   # scan specific paths
    python scripts/check_broken_imports.py --venv .venv # verify third-party imports too
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# Known-broken module prefixes: each one has broken test collection at some
# point in this repo's history. They are all also caught by the resolver
# below — the registry just makes failures loud, listable, and extensible.
KNOWN_BROKEN_IMPORTS: tuple[str, ...] = (
    # No storage/schema.py exists; schema lives in storage/models.py +
    # storage/schema.sql. This exact import once broke full-suite collection.
    "storage.schema",
)

# Third-party roots that may legitimately be absent from the lint venv —
# intentional optional extras, not accidental misses. Each entry must say
# why it is allowed to be missing.
KNOWN_VENV_ABSENT: tuple[str, ...] = (
    # Opt-in Telegram public-channel crawler: pulled only by the `telegram`
    # extra ([project.optional-dependencies].telegram) and enabled only via
    # TELEGRAM_ENABLED=true; the lint venv (`pip install -e ".[dev]"`)
    # intentionally does not install it.
    "telethon",
)

# Top-level stdlib module names (py3.10+). ``import os`` / ``import sys``
# must never fail the venv check — stdlib lives outside site-packages and
# some members (sys, time, ...) are frozen with no file on disk at all.
_STDLIB_TOP_LEVEL: frozenset[str] = frozenset(sys.stdlib_module_names)

# Venv layout: ``<venv>/lib/python*/site-packages``. Third-party roots are
# resolved against this directory when --venv is given; unknown roots fail.


# Any .py file under one of these is skipped, wherever it appears.
SKIP_DIRS = {
    ".git",
    ".venv",
    ".serpent-circle",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _iter_py_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def _venv_site_packages(venv_root: Path) -> Path | None:
    """Resolve ``venv_root``'s site-packages directory, or None.

    Standard venv layout: ``<venv>/lib/python*/site-packages``; the first
    ``python*`` match wins. Pure path logic — the directory layout is what
    matters, nothing is imported.
    """
    lib = venv_root / "lib"
    if not lib.is_dir():
        return None
    for py in sorted(lib.glob("python*"), reverse=True):
        site = py / "site-packages"
        if site.is_dir():
            return site
    return None


def _resolve_import(module: str, repo: Path) -> str | None:
    """Return an error description if ``module`` doesn't resolve under ``repo``.

    ``module``'s first component must exist under ``repo`` (the caller only
    routes repo-local roots here). Each further component must be a
    subdirectory or ``<component>.py``; a plain-module root can't have
    submodules. Returns None when the import resolves.
    """
    parts = module.split(".")
    current = repo / parts[0]
    if not current.exists():
        return None  # third-party root, not verifiable here
    if current.is_file():
        rest = ".".join(parts[1:])
        return (
            None
            if not rest
            else (f"`{parts[0]}.py` is a plain module; it cannot be a package for `{rest}`")
        )
    for idx, comp in enumerate(parts[1:], start=1):
        as_dir = current / comp
        if as_dir.is_dir():
            current = as_dir
            continue
        as_file = current / f"{comp}.py"
        if as_file.is_file():
            rest = ".".join(parts[idx + 1 :])
            return (
                None
                if not rest
                else (f"`{as_file.name}` is a module; nothing to resolve after `{rest}`")
            )
        return f"no `{comp}.py` or `{comp}/` under `{current.relative_to(repo)}/`"
    return None


def _module_error(
    module: str,
    repo: Path,
    *,
    site_packages: Path | None = None,
) -> tuple[bool, str] | None:
    """Broken-module reason for ``module``, or None when it's fine/unverifiable.

    Returns ``(is_known_broken, reason)``: ``is_known_broken`` marks a
    KNOWN_BROKEN_IMPORTS hit (callers phrase that differently from a tree
    resolver miss), and ``reason`` describes the problem. None means the
    name resolves against the repo tree — or, when ``site_packages`` is
    provided, against that venv (stdlib first, then site-packages) — or is
    a third-party root that can't be verified in scope (no ``--venv``).
    """
    for broken in KNOWN_BROKEN_IMPORTS:
        if module == broken or module.startswith(f"{broken}."):
            return (True, f"known-broken import ({broken})")
    root = module.split(".")[0]
    if not (repo / root).exists():
        if site_packages is None:
            return None  # third-party root, not verifiable without --venv
        if root in _STDLIB_TOP_LEVEL:
            return None  # stdlib — lives outside site-packages
        if root in KNOWN_VENV_ABSENT:
            return None  # intentional optional extra, absent by design
        if (site_packages / root).is_dir() or (site_packages / f"{root}.py").is_file():
            return None  # installed in the venv
        return (False, f"`{root}` not found in repo or venv site-packages")
    error = _resolve_import(module, repo)
    if error is None:
        return None
    return (False, error)


# Canonical callable forms treated as dynamic module-name loads. Each entry
# maps a descriptor string to the dotted attribute path it matches. The set
# covers every stdlib resolver that takes a dotted module name as its first
# argument: importlib.import_module, importlib.util.find_spec, the long-gone
# importlib.find_loader (removed in 3.12), and the pkgutil family
# (resolve_name, plus the find_loader / get_loader loader lookups deprecated
# in 3.12+). ``__import__`` is handled separately by its bare-name form (it
# is not a module member). Attributes are matched only against these full
# canonical paths — a bare name (``find_spec(...)``), a re-exported alias
# (``util.find_spec(...)`` after ``from importlib import util``), or any
# other binding could be a local shadow or wildcard import, so only the
# canonical-path forms are statically trustworthy; the string-literal module
# name is the only thing verifiable anyway. Add a form as one
# (descriptor, path) pair.
_DYNAMIC_IMPORT_FORMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("importlib.import_module", ("importlib", "import_module")),
    ("importlib.util.find_spec", ("importlib", "util", "find_spec")),
    ("importlib.find_loader", ("importlib", "find_loader")),
    ("pkgutil.resolve_name", ("pkgutil", "resolve_name")),
    ("pkgutil.find_loader", ("pkgutil", "find_loader")),
    ("pkgutil.get_loader", ("pkgutil", "get_loader")),
)


def _dynamic_import_descriptor(func: ast.expr) -> str | None:
    """Canonical descriptor for a recognized dynamic-load ``Call.func``.

    ``__import__`` is matched by its bare name; every other recognized form
    is an attribute chain whose full dotted path appears in
    ``_DYNAMIC_IMPORT_FORMS`` (walked outermost-to-innermost). Anything else
    — a different bare name, a shorter/unlisted path, a subscripted or bound
    target — returns None so the call is left unverified (matching the
    conservative shadowing rationale above).
    """
    if isinstance(func, ast.Name):
        return "__import__" if func.id == "__import__" else None
    if not isinstance(func, ast.Attribute):
        return None
    parts: list[str] = []
    current: ast.expr = func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    path = tuple(reversed(parts))
    for descriptor, candidate in _DYNAMIC_IMPORT_FORMS:
        if path == candidate:
            return descriptor
    return None


def _dynamic_import_module(node: ast.Call) -> tuple[str, ast.Constant] | None:
    """Extract ``(descriptor, name_argument)`` from a dynamic-import Call.

    Matches the stdlib dynamic-load forms (``importlib.import_module``,
    ``importlib.util.find_spec``, the ``pkgutil`` resolvers, ``__import__``)
    when the module name is a string-literal constant, given positionally or
    as the ``name=`` keyword. Returns None for any other call and —
    deliberately — for variable/expression name arguments, which can't be
    validated.
    """
    descriptor = _dynamic_import_descriptor(node.func)
    if descriptor is None:
        return None
    name_arg: ast.expr | None = None
    if node.args:
        name_arg = node.args[0]
    else:
        for kw in node.keywords:
            if kw.arg == "name":
                name_arg = kw.value
                break
    if not isinstance(name_arg, ast.Constant) or not isinstance(name_arg.value, str):
        return None
    return descriptor, name_arg


def _check_dynamic_import(
    descriptor: str,
    name_arg: ast.Constant,
    lineno: int,
    path: Path,
    repo: Path,
    *,
    site_packages: Path | None = None,
) -> list[str]:
    """Error messages for one dynamic-load call of a broken module name."""
    module = str(name_arg.value)
    call = f'{descriptor}("{module}")'
    broken = _module_error(module, repo, site_packages=site_packages)
    # pkgutil.resolve_name uniquely accepts ``module.attr`` references, so a
    # non-resolving full string may still be a resolvable module plus a
    # legitimate attribute tail. When it fails through the RESOLVER only
    # (never the known-broken registry), strip the final segment and, if the
    # module prefix resolves, leave the attribute candidate unverified.
    if (
        broken is not None
        and not broken[0]
        and descriptor == "pkgutil.resolve_name"
        and "." in module
        and _module_error(module.rsplit(".", 1)[0], repo, site_packages=site_packages) is None
    ):
        return []
    if broken is None:
        return []
    is_known, reason = broken
    if is_known:
        return [f"{path}:{lineno}: {call} — {reason}; fix the reference before committing"]
    return [f"{path}:{lineno}: {call} — {reason} (module would not resolve at runtime)"]


def _check_module_name(
    module: str,
    lineno: int,
    path: Path,
    repo: Path,
    *,
    site_packages: Path | None = None,
) -> list[str]:
    """Return error messages for one static dotted import name, or an empty list.

    A static import runs at module load, so a broken reference here breaks
    pytest collection for the whole suite.
    """
    broken = _module_error(module, repo, site_packages=site_packages)
    if broken is None:
        return []
    is_known, reason = broken
    if is_known:
        return [f"{path}:{lineno}: import {module} — {reason}; fix the reference before committing"]
    return [f"{path}:{lineno}: import {module} — {reason} (would break pytest collection)"]


def scan_files(
    files: list[Path],
    repo: Path,
    *,
    site_packages: Path | None = None,
) -> list[str]:
    issues: list[str] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            issues.append(f"{path}: could not parse — {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    issues.extend(
                        _check_module_name(
                            alias.name, node.lineno, path, repo, site_packages=site_packages
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import — resolves within its own package
                if node.module:
                    issues.extend(
                        _check_module_name(
                            node.module, node.lineno, path, repo, site_packages=site_packages
                        )
                    )
            elif isinstance(node, ast.Call):
                dynamic = _dynamic_import_module(node)
                if dynamic is not None:
                    descriptor, name_arg = dynamic
                    issues.extend(
                        _check_dynamic_import(
                            descriptor,
                            name_arg,
                            node.lineno,
                            path,
                            repo,
                            site_packages=site_packages,
                        )
                    )
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flag imports that would break pytest collection")
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="PATH",
        help="scan only this file/dir (repeatable); default: the whole repo",
    )
    parser.add_argument(
        "--venv",
        metavar="PATH",
        default=None,
        help="resolve third-party imports against this venv (stdlib + site-packages)",
    )
    args = parser.parse_args(argv)

    repo = _repo_root()
    venv_root = Path(args.venv) if args.venv else None
    if venv_root is not None and not venv_root.is_absolute():
        venv_root = repo / venv_root  # resolve like --path does
    site_packages = _venv_site_packages(venv_root) if venv_root is not None else None
    if args.venv and site_packages is None:
        print(
            f"warning: no site-packages found under --venv {args.venv}; "
            "third-party imports will not be verified",
            file=sys.stderr,
        )
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

    issues = scan_files(files, repo, site_packages=site_packages)
    if not issues:
        print(f"broken-import scan OK — {len(files)} .py file(s) checked")
        return 0
    print(
        "broken-import scan FAILED — static imports would break pytest collection;\n"
        "dynamic loads would fail at runtime:\n"
    )
    for issue in issues:
        print(f"  {issue}")
    print(
        "\nCheck the dotted path against the repo tree "
        "(e.g. schema lives in storage/models.py, not storage/schema.py)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
