from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from common.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None):
    settings = get_settings()
    url = database_url or settings.database_url
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
    if "sqlite" not in settings.database_url:
        return None
    lock_path = Path(f"{settings.database_url.replace('sqlite:///', '')}.lock")
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
