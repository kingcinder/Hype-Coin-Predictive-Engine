"""``Settings`` now owns the ``SERPENT_DB_PATH`` / ``DATABASE_URL`` env override.

Previously only the storage layer (``storage.database.resolve_database_url``)
honored the throwaway-DB env overrides, so every consumer that read
``settings.database_url`` directly — diagnose_retention's ``--db`` default,
bootstrap_local's written-config banner, alembic migrations, the watchdog's
sqlite checks — silently bound the *configured* DB. The override resolution
now lives in ``common.config`` (``apply_database_url_env_override`` +
``database_url_env_override``) so ``settings.database_url`` is the effective
URL for every consumer, not just ``session_scope()`` users.

Precedence (mirroring the old storage contract):

1. ``SERPENT_DB_PATH`` beats everything — even an explicit ``database_url=``
   init kwarg; a project-specific throwaway path must never lose the write.
2. ``DATABASE_URL`` applies unless an explicit ``database_url=`` kwarg was
   given (pydantic's ``init > env`` priority).
3. Otherwise the configured/default URL stands.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from common.config import Settings, get_settings
from storage.database import make_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGURED_URL = "postgresql+psycopg://serpent:serpent@localhost:5432/serpent"


@pytest.fixture(autouse=True)
def _isolate_settings_cache() -> None:
    """Clear the ``get_settings`` lru-cache around every test in this module.

    The storage engine is constructed once at import from whatever the env
    held then, so clearing the cache here only forces fresh ``Settings``
    construction for this module's own ``get_settings`` assertions — it never
    rebinds the module-level engine mid-suite.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Settings-level precedence & normalization (in-process)
# ---------------------------------------------------------------------------


def test_serpent_db_path_bare_path_normalizes_to_abs_sqlite_url(monkeypatch, tmp_path) -> None:
    target = tmp_path / "throwaway.db"
    monkeypatch.setenv("SERPENT_DB_PATH", str(target))

    settings = Settings(_env_file=None)

    assert settings.database_url == f"sqlite:///{target}"
    assert settings.database_url_env == "SERPENT_DB_PATH"


def test_serpent_db_path_full_url_passes_through(monkeypatch, tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'throwaway.db'}"
    monkeypatch.setenv("SERPENT_DB_PATH", url)

    settings = Settings(_env_file=None)

    assert settings.database_url == url


def test_serpent_db_path_normalizes_tilde_and_relative_paths(monkeypatch) -> None:
    monkeypatch.setenv("SERPENT_DB_PATH", "~/serpent-test.db")

    settings = Settings(_env_file=None)

    assert settings.database_url == f"sqlite:///{Path.home() / 'serpent-test.db'}"


def test_database_url_env_applies_when_not_explicitly_set(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@db:5432/x")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://user:pass@db:5432/x"
    assert settings.database_url_env == "DATABASE_URL"


def test_database_url_env_yields_to_explicit_init_kwarg(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@db:5432/x")

    settings = Settings(_env_file=None, database_url="sqlite:///explicit.db")

    assert settings.database_url == "sqlite:///explicit.db"


def test_serpent_db_path_beats_database_url_env(monkeypatch, tmp_path) -> None:
    target = tmp_path / "serpent-wins.db"
    monkeypatch.setenv("SERPENT_DB_PATH", str(target))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@db:5432/x")

    settings = Settings(_env_file=None)

    assert settings.database_url == f"sqlite:///{target}"
    assert settings.database_url_env == "SERPENT_DB_PATH"


def test_serpent_db_path_beats_explicit_init_kwarg(monkeypatch, tmp_path) -> None:
    """SERPENT_DB_PATH is project-specific and must always win, per the old
    storage-layer contract — even over a caller's explicit ``database_url=``."""
    target = tmp_path / "serpent-wins.db"
    monkeypatch.setenv("SERPENT_DB_PATH", str(target))

    settings = Settings(_env_file=None, database_url=CONFIGURED_URL)

    assert settings.database_url == f"sqlite:///{target}"


def test_no_override_keeps_configured_url(monkeypatch) -> None:
    monkeypatch.delenv("SERPENT_DB_PATH", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.database_url == CONFIGURED_URL
    assert settings.database_url_env is None


def test_local_single_profile_yields_to_serpent_override(monkeypatch, tmp_path) -> None:
    """The zero-container profile default (``sqlite:///serpent.db``) must NOT
    clobber the env override — a throwaway-DB run stays bound to its target."""
    target = tmp_path / "throwaway.db"
    monkeypatch.setenv("SERPENT_DB_PATH", str(target))

    settings = Settings(_env_file=None, env="local-single")

    assert settings.database_url == f"sqlite:///{target}"


def test_get_settings_reflects_override_after_cache_clear(monkeypatch, tmp_path) -> None:
    """The cached settings object resolves the override exactly like a fresh
    one — the single source of truth must not be bypassable through the cache."""
    target = tmp_path / "cached-throwaway.db"
    monkeypatch.setenv("SERPENT_DB_PATH", str(target))

    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.database_url == f"sqlite:///{target}"
        assert settings.database_url_env == "SERPENT_DB_PATH"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# End-to-end: consumers that read settings.database_url directly
# ---------------------------------------------------------------------------


def _scrubbed_env(**overrides: str) -> dict[str, str]:
    env = {
        **os.environ,
        # kill any ambient override so the test is hermetically pinned to the
        # explicit SERPENT_DB_PATH below ('' is falsy after .strip()).
        "DATABASE_URL": "",
        "ENV": "local-single",
        "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    env.update(overrides)
    return env


def test_diagnose_retention_db_default_reflects_override(tmp_path) -> None:
    """diagnose_retention's ``--db`` flag defaults to ``settings.database_url``
    — which must be the override URL, not the configured one, so the diagnostic
    always examines the DB the engine is actually bound to.

    Argparse omits ``default:`` from short help unless the help string embeds
    ``%(default)s``, so the assertion targets the script's ``Database :`` banner
    — printed before any DB query — which echoes the effective ``--db``."""
    target = tmp_path / "wedged.db"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.diagnose_retention"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
        env=_scrubbed_env(SERPENT_DB_PATH=str(target)),
    )

    # The throwaway DB is empty (no retention_runs table), so the diagnostic
    # reports the query failure and exits 1 — irrelevant here: the banner is
    # printed before any query and must advertise the override URL.
    assert result.returncode in (0, 1)
    assert f"Database         : sqlite:///{target}" in result.stdout
    assert CONFIGURED_URL not in result.stdout


def test_bootstrap_local_writes_config_for_override_db(tmp_path) -> None:
    """bootstrap_local's schema + seed + printed-banner must all target the
    SERPENT_DB_PATH file, and its banner must advertise that effective URL."""
    target = tmp_path / "bootstrap-throwaway.db"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "bootstrap_local.py")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=tmp_path,  # keep repo clean; the script self-inserts repo root on sys.path
        env=_scrubbed_env(SERPENT_DB_PATH=str(target), ENV="local-single"),
    )

    assert result.returncode == 0, result.stderr
    assert f"Database : sqlite:///{target}" in result.stdout
    # The override DB really took the writes — schema exists and reference data
    # landed, proving the whole flow bound to the throwaway path, not serpent.db.
    assert target.exists()
    conn = sqlite3.connect(target)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        chains = conn.execute("SELECT count(*) FROM chains").fetchone()[0]
    finally:
        conn.close()
    assert {"chains", "assets", "sources"} <= tables
    assert chains >= 3  # solana / base / ethereum
    # bootstrap_local now runs the shared alembic seam (not create_all only), so
    # alembic_version is stamped and migration-owned tables exist — the
    # guarantee that a worker-only local deploy has score_drift_runs (0020)
    # before the first drift probe, even without the combined engine. The head
    # revision is deliberately NOT pinned: the requirement is the seam reaches
    # whatever head is current (a pin would break on the next migration).
    conn = sqlite3.connect(target)
    try:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()
    assert version
    assert "score_drift_runs" in tables


# ---------------------------------------------------------------------------
# Boot-loud override warning (engine-bound redirect must never be silent)
#
# NOTE: this repo's structlog is configured with a PrintLoggerFactory (no
# stdlib logger_factory), so warnings render as JSON straight to stdout and
# never reach caplog — capture via capsys instead.
# ---------------------------------------------------------------------------


def _warning_events(out: str) -> list[str]:
    return [line for line in out.splitlines() if "url_overridden" in line]


def test_make_engine_logs_url_overridden_for_serpent(monkeypatch, capsys, tmp_path) -> None:
    """A SERPENT_DB_PATH-bound engine must emit a ``url_overridden`` warning
    naming the source — a throwaway bind at boot is a deliberate redirect and
    must show up in the log, never silently repoint the engine."""
    target = tmp_path / "override.db"
    monkeypatch.setenv("SERPENT_DB_PATH", str(target))

    engine = make_engine()
    out = capsys.readouterr().out

    assert str(engine.url).startswith("sqlite:///")
    events = _warning_events(out)
    assert len(events) == 1
    assert "SERPENT_DB_PATH" in events[0]
    assert f"sqlite:///{target}" in events[0]


def test_make_engine_logs_url_overridden_for_database_url(monkeypatch, capsys) -> None:
    """Same guard for the generic 12-factor var — a stray DATABASE_URL in the
    environment that repoints a boot must be loud."""
    override_url = "postgresql+psycopg://user:pass@db:5432/x"
    monkeypatch.setenv("DATABASE_URL", override_url)

    engine = make_engine()
    out = capsys.readouterr().out

    # str(engine.url) masks the password (user:***); render raw for equality.
    assert engine.url.render_as_string(hide_password=False) == override_url
    events = _warning_events(out)
    assert len(events) == 1
    assert "DATABASE_URL" in events[0]
    assert override_url in events[0]


def test_make_engine_silent_without_override(capsys) -> None:
    """With no override in the environment the engine binds the configured URL
    and must NOT warn — silence is the default, so any warning is meaningful."""
    make_engine()

    assert _warning_events(capsys.readouterr().out) == []


def test_make_engine_explicit_url_is_exempt(monkeypatch, capsys, tmp_path) -> None:
    """A caller passing an explicit ``database_url=`` pins the target
    deliberately — even with an override set, that engine must not warn (the
    override was not honored for it)."""
    explicit = f"sqlite:///{tmp_path / 'explicit.db'}"
    monkeypatch.setenv("SERPENT_DB_PATH", str(tmp_path / "override.db"))

    engine = make_engine(database_url=explicit)

    assert str(engine.url) == explicit
    assert _warning_events(capsys.readouterr().out) == []
