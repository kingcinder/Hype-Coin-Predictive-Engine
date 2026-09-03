from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from common.config import Settings, database_url_env_override, get_settings
from common.logging import get_logger

log = get_logger(__name__)


def run_migrations() -> None:
    """Apply the alembic migration chain to the configured database.

    Single shared migration seam for every boot path — the combined engine
    (``python -m engine``), the standalone worker (``python -m ingestion.worker
    --loop``, which the systemd unit runs), and the local bootstrap
    (``scripts/bootstrap_local.py``). ``Base.metadata.create_all`` only creates
    missing *tables* — it never alters existing tables when models gain columns,
    and it never stamps ``alembic_version``. Every migration in the chain is
    existence-guarded, so ``upgrade head`` is idempotent: fresh DBs build the
    full schema, existing DBs get only the pending column/table additions
    (e.g. 0020's ``score_drift_runs`` must exist before the worker's first
    drift probe).
    """
    try:
        from alembic import command
        from alembic.config import Config

        project_root = Path(__file__).resolve().parents[1]
        cfg = Config(str(project_root / "storage" / "alembic.ini"))
        cfg.set_main_option("script_location", str(project_root / "storage" / "migrations"))
        command.upgrade(cfg, "head")
        log.info("migrations_applied")
    except Exception as exc:  # noqa: BLE001 - never block boot on migration issues.
        log.warning("migrations_failed", error=str(exc))


class Base(DeclarativeBase):
    pass


def resolve_database_url(settings: Settings) -> str:
    """Effective DB URL for a given ``Settings`` instance.

    The ``SERPENT_DB_PATH`` / ``DATABASE_URL`` override is now owned by
    ``Settings`` itself (``apply_database_url_env_override`` /
    ``database_url_env_override`` in ``common.config``): every consumer that
    reads ``settings.database_url`` — session_scope() users, alembic
    migrations, diagnose_retention's ``--db`` default, bootstrap_local's
    written config, the watchdog's sqlite checks — binds the effective URL,
    so a throwaway-DB run is uniformly visible instead of only through this
    module. This accessor exists for callers that already hold a Settings
    instance; an explicit ``database_url=`` argument to ``make_engine`` still
    wins over it (callers win).

    Precedence (resolved once, in ``Settings``):

    1. ``SERPENT_DB_PATH`` — a filesystem path to a SQLite file (or a full
       ``scheme://`` URL), project-specific and unambiguous;
    2. ``DATABASE_URL`` — the generic 12-factor connection URL;
    3. ``settings.database_url`` — the profile-resolved configured value.
    """
    return settings.database_url


def make_engine(database_url: str | None = None):
    settings = get_settings()
    url = database_url or resolve_database_url(settings)
    # Boot-loud override guard: an env override silently repoints every engine
    # built from the effective URL, so a stale exporter (e.g. a stray
    # DATABASE_URL in a production shell) must never redirect a boot without a
    # trace. Say so the moment it happens. Explicit caller-provided URLs are
    # exempt: the caller pinned the target deliberately.
    if database_url is None:
        override = database_url_env_override()
        if override is not None and settings.database_url == override[1]:
            log.warning("url_overridden", source=override[0], effective_url=url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        # The single-command engine runs the worker and the API against the same
        # SQLite file from two threads. WAL mode + a generous busy timeout keep
        # concurrent reads and writes from colliding with spurious
        # "database is locked" errors.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_ms)}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def acquire_sqlite_writer_lock(settings: Settings | None = None) -> int | None:
    """Advisory single-writer guard for the SQLite engine file.

    Two engine processes writing one ``.db`` file contend for SQLite's single
    write lock and wedge the loop on "database is locked". This acquires an
    exclusive, non-blocking file lock on a sibling ``<db>.lock`` file and holds
    it for the process lifetime (the OS releases it automatically on exit or
    crash, so there is never a stale lock). Returns the open lock fd, or ``None``
    for non-SQLite backends (Postgres handles many writers itself).

    Raises ``RuntimeError`` if another process already holds the lock, so a
    second engine fails fast at boot with a clear message instead of wedging.
    """
    settings = settings or get_settings()
    url = resolve_database_url(settings)
    if "sqlite" not in url:
        return None
    lock_path = Path(f"{url.replace('sqlite:///', '')}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if os.path.getsize(lock_path) == 0:
            os.write(fd, b"lock")
        if os.name == "nt":  # pragma: no cover - exercised on Windows hosts.
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError(
            f"another process already holds the SQLite writer lock "
            f"({lock_path}); refusing to start. Keep exactly one writer per "
            "serpent.db — a second writer causes 'database is locked' loop "
            "wedges."
        ) from None
    return fd


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Explicit context-manager session for ``with session_scope() as s:`` callers.

    FastAPI's ``Depends`` requires ``get_session`` to stay a *bare* generator
    (FastAPI drives ``next()``/``throw()`` on the generator object itself — a
    ``@contextmanager`` wrapper breaks every DB endpoint with
    ``TypeError: '_GeneratorContextManager' object is not an iterator``).
    Non-FastAPI code that wants ``with`` syntax must use this dedicated
    context manager instead of wrapping ``get_session``.
    """
    with SessionLocal() as session:
        yield session


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: yield a session, closed on request teardown.

    Intentionally a bare generator — ``Depends`` drives it directly. Use
    ``session_scope()`` for ``with``-style call sites.
    """
    with SessionLocal() as session:
        yield session
