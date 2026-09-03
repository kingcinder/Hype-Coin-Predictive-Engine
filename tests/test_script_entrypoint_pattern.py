"""Collection-time guard for the ``scripts/`` entry-point session pattern.

Every ``scripts/*.py`` that defines an ``if __name__ == "__main__":`` block is
AST-introspected (no imports executed, no DB bound) as it is *discovered* —
the file list and parses are computed when this module is imported, i.e.
during pytest collection — and three contract rules are enforced so a future
migration script that drifts from the established pattern fails the suite
immediately:

1. **Keyword-only ``session=None`` seams** — any function parameter named
   ``session`` that defaults to ``None`` must appear after a bare ``*``
   (``*, session=None``), the signature ``backfill_coingecko`` /
   ``backfill_defillama`` / ``seed_fixture_data`` follow and that
   ``rescore`` was aligned to. A positional-or-keyword seam would let the CLI
   path accumulate positional arguments, blurring the boundary the injectable
   seam is meant to keep sharp. (Required, caller-owned ``session`` params on
   the ``_*_in_session`` body functions carry no default and are exempt — that
   is the body pattern.)

2. **No module-level ``SessionLocal`` / ``engine`` bindings** — neither an
   assignment (``engine = create_engine(...)``, ``SessionLocal =
   sessionmaker(...)``) nor a module-level ``from storage.database import
   SessionLocal, engine`` is allowed: either binds the configured DB at
   *script-import* time, which is exactly the untestable-CLI behavior
   ``SERPENT_DB_PATH`` was added to remove. ``bootstrap_local.py`` was aligned
   to import ``session_scope`` only. Call-time construction stays legal:
   ``diagnose_retention.py`` builds a read-only ``NullPool`` engine inside its
   ``_assess`` for an arbitrary ``--db`` URL, which is its entire purpose.

3. **Session-family scripts route their entry through ``session_scope()``** —
   when a script defines a seam or references ``session_scope`` anywhere, the
   functions called from its ``__main__`` block must transitively reach a
   ``session_scope(...)`` call. Scripts that never open a SQLAlchemy session
   (``backup.py`` uses the stdlib ``sqlite3`` online-backup API; the import
   linters scan text; ``diagnose_retention`` binds a caller-provided engine by
   design) carry no seam and are exempt from this rule — but they are still
   held to rules 1 and 2, which hold universally.

The helpers below return violations as plain data so the guard can be
regression-pinned: each rule has an adversary-snippet test proving it *can*
fire, plus a known-family pin proving the established scripts are still
detected as session-family and route correctly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"

# Names whose *module-level* binding breaks the migration pattern (binds the
# configured DB at import time, defeating the SERPENT_DB_PATH override
# contract and the injectable-seam boundary).
FORBIDDEN_MODULE_NAMES = frozenset({"SessionLocal", "engine"})

# The established session-family: scripts that define a session=None seam or
# route through session_scope() — the ones the routing rule must police.
KNOWN_SESSION_FAMILY = frozenset(
    {"rescore.py", "backfill_history.py", "seed_fixtures.py", "bootstrap_local.py"}
)


# ---------------------------------------------------------------------------
# AST helpers (pure — no imports executed, safe on adversarial snippets)
# ---------------------------------------------------------------------------


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _find_main_block(tree: ast.Module) -> ast.If | None:
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            return node
    return None


def _is_none_constant(node: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _calls_name(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name
        for call in ast.walk(node)
    )


def _session_seams(tree: ast.Module) -> list[tuple[str, bool]]:
    """All ``session=None`` seams as (function, keyword_only)."""
    seams: list[tuple[str, bool]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = fn.args.posonlyargs + fn.args.args
        defaults = [None] * (len(positional) - len(fn.args.defaults)) + list(fn.args.defaults)
        for param, default in zip(positional, defaults, strict=False):
            if param.arg == "session" and _is_none_constant(default):
                seams.append((fn.name, False))
        for param, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=False):
            if param.arg == "session" and _is_none_constant(default):
                seams.append((fn.name, True))
    return seams


def _module_level_forbidden_bindings(tree: ast.Module) -> list[str]:
    """Module-level ``SessionLocal``/``engine`` assignments or imports."""
    violations: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for node in ast.walk(target):
                    if (
                        isinstance(node, ast.Name)
                        and node.id in FORBIDDEN_MODULE_NAMES
                        and isinstance(node.ctx, ast.Store)
                    ):
                        violations.append(node.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id in FORBIDDEN_MODULE_NAMES:
                violations.append(stmt.target.id)
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "storage.database":
            for alias in stmt.names:
                if alias.name in FORBIDDEN_MODULE_NAMES:
                    violations.append(f"{stmt.module}.{alias.name}")
    return violations


def _references_session_scope(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "session_scope":
            return True
        if isinstance(node, ast.alias) and node.name == "session_scope":
            return True
    return False


def _module_call_graph(tree: ast.Module) -> dict[str, set[str]]:
    """Map every def name -> bare-name calls in its *own* body.

    Nested function definitions are skipped: their callees belong to the
    nested def's own graph entry, so a ``session_scope`` call tucked inside an
    unreachable nested helper can never make an entry point look routed.
    """

    def _callees(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # nested def → attributed to its own entry
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                found.add(child.func.id)
            found |= _callees(child)
        return found

    graph: dict[str, set[str]] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            graph[fn.name] = _callees(fn)
    return graph


def _entry_reaches_session_scope(tree: ast.Module) -> bool:
    """True when the ``__main__`` entry transitively calls ``session_scope``."""
    main_block = _find_main_block(tree)
    if main_block is None:
        return False
    if _calls_name(main_block, "session_scope"):
        return True
    graph = _module_call_graph(tree)
    entries = {
        call.func.id
        for call in ast.walk(main_block)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }
    reachable: set[str] = set()
    frontier = set(entries)
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        frontier |= graph.get(name, set())
    return any("session_scope" in graph.get(name, set()) for name in reachable)


# ---------------------------------------------------------------------------
# Discovery (runs at collection time)
# ---------------------------------------------------------------------------


def _discover_main_scripts() -> list[tuple[str, ast.Module]]:
    out: list[tuple[str, ast.Module]] = []
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _find_main_block(tree) is not None:
            out.append((path.name, tree))
    return out


# Evaluated when this module is imported by pytest → the parametrize list is
# fixed at collection, so a script added/renamed/removed is swept automatically
# and a *syntax* error in any scripts/*.py fails collection loudly.
_MAIN_SCRIPTS = _discover_main_scripts()
_SCRIPT_IDS = [name for name, _ in _MAIN_SCRIPTS]


# ---------------------------------------------------------------------------
# The contract, parametrized over every __main__ script
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, tree", _MAIN_SCRIPTS, ids=_SCRIPT_IDS)
def test_session_none_seams_keyword_only(name: str, tree: ast.Module) -> None:
    """Rule 1: every ``session=None`` seam is ``*, session=None``."""
    offenders = [fn for fn, kwonly in _session_seams(tree) if not kwonly]
    assert not offenders, (
        f"{name}: session=None seams must be keyword-only (add a bare '*' before them): {offenders}"
    )


@pytest.mark.parametrize("name, tree", _MAIN_SCRIPTS, ids=_SCRIPT_IDS)
def test_no_module_level_session_machinery_bindings(name: str, tree: ast.Module) -> None:
    """Rule 2: no module-scope SessionLocal/engine assignment or import."""
    binds = _module_level_forbidden_bindings(tree)
    assert not binds, (
        f"{name}: module-level {binds} binding(s) — import session_scope from "
        "storage.database instead of binding the configured DB at import time"
    )


@pytest.mark.parametrize("name, tree", _MAIN_SCRIPTS, ids=_SCRIPT_IDS)
def test_session_family_entry_routes_through_session_scope(name: str, tree: ast.Module) -> None:
    """Rule 3: session-family scripts reach ``session_scope()`` from __main__."""
    seams = _session_seams(tree)
    if not seams and not _references_session_scope(tree):
        pytest.skip(
            f"{name} opens no SQLAlchemy session (no seam / no session_scope); "
            "routing rule not applicable"
        )
    assert _entry_reaches_session_scope(tree), (
        f"{name} defines session machinery but its __main__ entry never "
        "transitively reaches session_scope() — the CLI path must open and own "
        "its own session_scope() cycle (see scripts/rescore.py)"
    )


# ---------------------------------------------------------------------------
# Guard-the-guard: the rules themselves must be provably able to fire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("signature", "violates_rule_1"),
    [
        pytest.param("def migrate(dry_run=False, session=None): pass", True, id="positional-seam"),
        pytest.param("def migrate(*, dry_run=False, session=None): pass", False, id="kwonly-seam"),
        pytest.param("def _body(session): pass", False, id="required-positional-body-param-exempt"),
    ],
)
def test_seam_keyword_only_rule_fires(signature: str, violates_rule_1: bool) -> None:
    tree = ast.parse(f"{signature}\n\nif __name__ == '__main__':\n    migrate()\n")
    seams = _session_seams(tree)
    # any(not kwonly ...): a non-empty offender LIST is truthy even when it
    # holds [False], so compare the logical predicate, not list truthiness.
    assert any(not kwonly for _, kwonly in seams) is violates_rule_1


@pytest.mark.parametrize(
    ("snippet", "expected_binding"),
    [
        pytest.param("engine = create_engine('sqlite:///x.db')", "engine", id="engine-assign"),
        pytest.param(
            "SessionLocal = sessionmaker(bind=engine)", "SessionLocal", id="sessionlocal-assign"
        ),
        pytest.param(
            "from storage.database import SessionLocal",
            "storage.database.SessionLocal",
            id="sessionlocal-import",
        ),
        pytest.param(
            "from storage.database import engine", "storage.database.engine", id="engine-import"
        ),
        pytest.param(
            "from storage.database import session_scope", None, id="session-scope-import-ok"
        ),
    ],
)
def test_module_binding_rule_fires(snippet: str, expected_binding: str | None) -> None:
    tree = ast.parse(f"{snippet}\n\nif __name__ == '__main__':\n    main()\n")
    binds = _module_level_forbidden_bindings(tree)
    if expected_binding is None:
        assert binds == []
    else:
        assert expected_binding in binds


def test_routing_rule_fires_on_bypass() -> None:
    """A seam script whose entry opens SessionLocal directly must fail rule 3."""
    tree = ast.parse(
        "from storage.database import SessionLocal\n\n"
        "def migrate(*, session=None):\n"
        "    return session\n\n"
        "if __name__ == '__main__':\n"
        "    with SessionLocal() as s:\n"
        "        migrate(session=s)\n"
    )
    assert _session_seams(tree)  # it is session-family...
    assert not _entry_reaches_session_scope(tree)  # ...but never routes via session_scope


def test_routing_rule_passes_on_own_cycle() -> None:
    """The established own-cycle shape passes rule 3 transitively."""
    tree = ast.parse(
        "from storage.database import session_scope\n\n"
        "def _run(active):\n"
        "    return active.commit()\n\n"
        "def migrate(*, session=None):\n"
        "    if session is not None:\n"
        "        return _run(session)\n"
        "    with session_scope() as active:\n"
        "        return _run(active)\n\n"
        "if __name__ == '__main__':\n"
        "    migrate()\n"
    )
    assert _session_seams(tree)
    assert _entry_reaches_session_scope(tree)


# ---------------------------------------------------------------------------
# Discovery pins: the guard must never silently stop covering its family
# ---------------------------------------------------------------------------


def test_known_session_family_still_detected_and_routing() -> None:
    trees = dict(_MAIN_SCRIPTS)
    missing = KNOWN_SESSION_FAMILY - set(trees)
    assert not missing, f"discovery lost scripts: {missing}"
    for name in sorted(KNOWN_SESSION_FAMILY):
        tree = trees[name]
        assert _session_seams(tree) or _references_session_scope(tree), (
            f"{name} is pinned as session-family but no seam/session_scope was found"
        )
        assert _entry_reaches_session_scope(tree), (
            f"{name} must keep routing its __main__ entry through session_scope()"
        )
