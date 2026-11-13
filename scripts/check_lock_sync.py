"""CI lock-sync guard: ``requirements-dev.txt`` must match ``pyproject.toml``.

History: the lock is authoritative for every dev/test install
(``scripts/install_dev_deps.sh`` installs it with ``-r``, then the project
editable with ``--no-deps``), so pip never re-resolves from the pyproject
floors. That is deliberate — it is what keeps SQLAlchemy 2.1 out of the gate —
but it also means a dependency added to ``pyproject.toml`` without a matching
pin in the lock is invisible until install time: ``pip check`` (or the first
``import``) fails somewhere far from the edit. Worse, a pin that drifts outside
its declared range (a stale lock, or a hand-edited version) installs cleanly and
then contradicts the compatibility contract the floors document.

What it does
------------
1. Parses ``[project.dependencies]`` and the selected optional-dependency
   groups (``--extra``, default ``dev`` — the groups the lock is meant to
   cover; ``telegram`` is an install-time extra, not part of the dev lock) from
   ``pyproject.toml``.
2. Parses the lock's ``name==version`` pins, rejecting unpinned and duplicate
   lines.
3. Fails when a declared requirement is missing from the lock, or when its pin
   does not satisfy its declared specifier.

PEP 440 handling is a deliberately small, documented subset: release-segment
comparison for ``==`` / ``===`` / ``!=`` / ``<`` / ``<=`` / ``>`` / ``>=`` /
``~=``. That covers every specifier this project declares. Anything else
wildcards, epochs beyond the release segment, or pre/post-release qualifiers —
is reported as unsupported rather than passed silently, so the guard never
claims a bound it did not actually check.

Stdlib-only on purpose, like the other lint guards: ``tomllib`` parses
``pyproject.toml`` (requires-python is >=3.12), so the scan runs on a bare
checkout before ``make setup``.

Usage:
    python scripts/check_lock_sync.py
    python scripts/check_lock_sync.py --extra dev --extra test
    python scripts/check_lock_sync.py --pyproject other/pyproject.toml --lock other/requirements.txt
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

# Allow direct execution (``python scripts/check_lock_sync.py``) to import the
# shared name normalizer from the sibling guard. Under pytest the repo root is
# already on sys.path, so this is a no-op there.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_declared_deps import _requirement_base_name  # noqa: E402

#: Optional-dependency groups the lock covers by default. ``dev`` is the group
#: ``scripts/install_dev_deps.sh`` installs; ``telegram`` stays an opt-in extra.
_DEFAULT_EXTRAS: tuple[str, ...] = ("dev",)

_REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
_SPECIFIER_RE = re.compile(r"^(===|==|!=|<=|>=|~=|<|>)\s*(\S+)$")
_PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)\s*$")


def _release(version: str) -> tuple[int, ...] | None:
    """Release segment of a version, or None when it cannot be compared.

    Strips an epoch (``1!``), a local version (``+local``) and any pre/post/dev
    qualifier (``2.0rc1`` -> ``2.0``). Comparing release segments only is what
    the documented subset promises; a version with no numeric part at all is
    unverifiable and reported as such.
    """
    core = version.split("+", 1)[0]
    core = re.sub(r"^[0-9]+!", "", core)
    match = re.match(r"\d+(?:\.\d+)*", core)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """Compare release tuples, padding the shorter one with zeros (2.0 == 2.0.0)."""
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return (padded_left > padded_right) - (padded_left < padded_right)


def _satisfies(version: str, specifier: str) -> bool | None:
    """Does ``version`` satisfy ``specifier``?

    Returns None when the specifier (or the version) falls outside the
    documented subset, so the caller can report it instead of guessing.
    """
    actual = _release(version)
    if actual is None:
        return None
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = _SPECIFIER_RE.match(clause)
        if match is None:
            return None
        operator, target_text = match.groups()
        if "*" in target_text:  # wildcard equality is outside the subset
            return None
        target = _release(target_text)
        if target is None:
            return None
        if operator in ("==", "==="):
            ok = _compare(actual, target) == 0
        elif operator == "!=":
            ok = _compare(actual, target) != 0
        elif operator == ">=":
            ok = _compare(actual, target) >= 0
        elif operator == "<=":
            ok = _compare(actual, target) <= 0
        elif operator == ">":
            ok = _compare(actual, target) > 0
        elif operator == "<":
            ok = _compare(actual, target) < 0
        else:  # ~=X.Y[.Z]: >= X.Y[.Z] and the same prefix as X.Y[.Z]
            ok = (
                _compare(actual, target) >= 0
                and _compare(actual[: len(target) - 1], target[:-1]) == 0
            )
        if not ok:
            return False
    return True


def _declared_requirements(pyproject: Path, extras: tuple[str, ...]) -> list[tuple[str, str]]:
    """``(name, specifier)`` for ``[project.dependencies]`` plus the given extras."""
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project", {}) or {}
    groups: list[list[str]] = [list(project.get("dependencies", []) or [])]
    optional = project.get("optional-dependencies", {}) or {}
    for extra in extras:
        if extra not in optional:
            raise SystemExit(
                f"pyproject.toml has no optional-dependencies group {extra!r}; "
                f"known groups: {', '.join(sorted(optional)) or '(none)'}"
            )
        groups.append(list(optional[extra] or []))

    requirements: list[tuple[str, str]] = []
    for group in groups:
        for requirement in group:
            text = str(requirement).split(";", 1)[0].strip()
            match = _REQUIREMENT_RE.match(text)
            if match is None:
                continue
            name = _requirement_base_name(text)
            if name is None:
                continue
            specifier = (match.group(3) or "").strip().strip("()").strip()
            requirements.append((name, specifier))
    return requirements


def _lock_pins(lock: Path) -> tuple[dict[str, tuple[str, int]], list[str]]:
    """Parse the lock into ``{name: (version, line_no)}`` plus line-level issues."""
    pins: dict[str, tuple[str, int]] = {}
    issues: list[str] = []
    for line_no, raw in enumerate(lock.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue  # blank, comment, or a pip option (``-r ...``)
        name = _requirement_base_name(line)
        match = _PIN_RE.match(line)
        if name is None or match is None:
            issues.append(f"{lock.name}:{line_no}: not pinned with '==' — {line!r}")
            continue
        if name in pins:
            issues.append(
                f"{lock.name}:{line_no}: duplicate pin for {name} "
                f"(first pinned on line {pins[name][1]})"
            )
            continue
        pins[name] = (match.group(2), line_no)
    return pins, issues


def _lock_sync_issues(
    pyproject: Path, lock: Path, *, extras: tuple[str, ...] = _DEFAULT_EXTRAS
) -> list[str]:
    """Every way the lock disagrees with the declarations, or an empty list."""
    requirements = _declared_requirements(pyproject, extras)
    pins, issues = _lock_pins(lock)
    for name, specifier in requirements:
        pin = pins.get(name)
        if pin is None:
            issues.append(
                f"pyproject.toml declares {name}{specifier} but {lock.name} has no pin "
                f"for it — add {name}==<version> to the lock"
            )
            continue
        version, line_no = pin
        if not specifier:
            continue
        satisfied = _satisfies(version, specifier)
        if satisfied is None:
            issues.append(
                f"{lock.name}:{line_no}: {name}=={version} cannot be checked against "
                f"the declared specifier {specifier!r} (version qualifiers/wildcards "
                f"are outside this guard's PEP 440 subset)"
            )
        elif not satisfied:
            issues.append(
                f"{lock.name}:{line_no}: {name}=={version} is outside the declared "
                f"range {specifier!r} (pyproject.toml) — update the pin or the floor"
            )
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when requirements-dev.txt drifts from the dependencies declared in pyproject.toml"
        )
    )
    parser.add_argument(
        "--pyproject",
        metavar="PATH",
        default=None,
        help="pyproject.toml to read declarations from (default: <repo>/pyproject.toml)",
    )
    parser.add_argument(
        "--lock",
        metavar="PATH",
        default=None,
        help="lock file to check (default: <repo>/requirements-dev.txt)",
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=None,
        metavar="GROUP",
        help=(
            "optional-dependencies group the lock covers (repeatable; "
            f"default: {', '.join(_DEFAULT_EXTRAS)})"
        ),
    )
    args = parser.parse_args(argv)

    repo = _REPO_ROOT
    pyproject = Path(args.pyproject) if args.pyproject else repo / "pyproject.toml"
    lock = Path(args.lock) if args.lock else repo / "requirements-dev.txt"
    for label, path in (("pyproject.toml", pyproject), ("lock file", lock)):
        if not path.is_file():
            print(f"lock-sync scan FAILED — {label} not found at {path}")
            return 1

    extras = tuple(args.extra) if args.extra else _DEFAULT_EXTRAS
    issues = _lock_sync_issues(pyproject, lock, extras=extras)
    if not issues:
        pins, _ = _lock_pins(lock)
        print(
            f"lock-sync scan OK — {len(_declared_requirements(pyproject, extras))} declared "
            f"requirement(s) (extras: {', '.join(extras)}) match {len(pins)} pin(s) in "
            f"{lock.name}"
        )
        return 0

    print(
        f"lock-sync scan FAILED — {lock.name} is out of sync with pyproject.toml "
        f"({len(issues)} issue(s)):\n"
    )
    for issue in issues:
        print(f"  {issue}")
    print(
        "\nUpdate the pin in requirements-dev.txt (or the declared range in "
        "pyproject.toml). Regenerate the lock only from a clean venv that already "
        "holds these versions — see the file header."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
