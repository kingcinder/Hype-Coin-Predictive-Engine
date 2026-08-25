"""Tests for the ensemble dirty-flag periodic flush persistence."""

from __future__ import annotations

import inspect
import time
from contextlib import AbstractContextManager, contextmanager

from sqlalchemy import select

from scoring.ensemble import EnsembleEngine
from storage import models

_THRESHOLD = 50
_INTERVAL = 300.0


def test_get_session_is_bare_generator_for_fastapi() -> None:
    """FastAPI's ``Depends`` drives bare generators via ``next()``/``throw()``.

    If ``get_session`` is ever decorated with ``@contextmanager`` again, every
    DB endpoint 500s with ``TypeError: '_GeneratorContextManager' object is
    not an iterator``. ``test_api.py`` overrides the dependency, so only this
    guard catches the regression — keep it.
    """
    from storage.database import get_session, session_scope

    assert inspect.isgeneratorfunction(get_session), (
        "get_session() must stay a bare generator for FastAPI Depends() — "
        "use session_scope() for with-style callers instead of decorating it."
    )
    # The other half of the contract: session_scope() must stay a real
    # context manager so ``with session_scope() as s:`` keeps working.
    # Calling a @contextmanager function returns the _GeneratorContextManager
    # without running the generator body — no session is opened here.
    assert isinstance(session_scope(), AbstractContextManager), (
        "session_scope() must stay a context manager so with-style callers "
        "(e.g. scoring/ensemble.py) keep working."
    )


def _patch_db_session(monkeypatch, session) -> None:
    """Point storage.database.session_scope at fresh sessions on the fixture engine.

    A FRESH session per call (rather than reusing the fixture session) is
    what makes the durability assertions meaningful: if ``_save_to_db``
    only flushed without committing, the write session's close would roll
    back the transaction and a subsequent fresh session would see nothing.
    """
    from sqlalchemy.orm import sessionmaker

    from storage import database

    maker = sessionmaker(
        bind=session.get_bind(), autoflush=False, autocommit=False, expire_on_commit=False
    )

    @contextmanager
    def _test_session():
        fresh = maker()
        try:
            yield fresh
        finally:
            fresh.close()

    monkeypatch.setattr(database, "session_scope", _test_session)


def _read_state_fresh(session) -> models.EnsembleState | None:
    """Read EnsembleState through a brand-new session to prove durability.

    A fresh session on the same engine only sees COMMITTED data — if a
    save was flushed but never committed, this returns None.
    """
    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(
        bind=session.get_bind(), autoflush=False, autocommit=False, expire_on_commit=False
    )
    with maker() as verify:
        return verify.scalar(select(models.EnsembleState).limit(1))


def _fresh_engine() -> EnsembleEngine:
    """Engine that skips DB load and starts with a clean dirty state."""
    engine = EnsembleEngine()
    engine._persisted = True
    engine._pending_outcomes = 0
    engine._last_flush = time.monotonic()
    return engine


def _record(engine: EnsembleEngine, count: int = 1) -> None:
    for _ in range(count):
        engine.record_outcome("rule", "GREEN", "positive")


def test_no_flush_below_outcome_threshold(monkeypatch) -> None:
    engine = _fresh_engine()
    saved = []
    monkeypatch.setattr(engine, "_save_to_db", lambda: saved.append(1))
    _record(engine, _THRESHOLD - 1)
    assert saved == []
    assert engine._pending_outcomes == _THRESHOLD - 1


def test_flush_exactly_at_outcome_threshold(monkeypatch) -> None:
    engine = _fresh_engine()
    saved = []
    monkeypatch.setattr(engine, "_save_to_db", lambda: saved.append(1))
    _record(engine, _THRESHOLD)
    assert len(saved) == 1


def test_flush_on_time_interval(monkeypatch) -> None:
    engine = _fresh_engine()
    saved = []
    monkeypatch.setattr(engine, "_save_to_db", lambda: saved.append(1))
    engine._last_flush = time.monotonic() - (_INTERVAL + 1.0)
    engine.record_outcome("rule", "GREEN", "positive")
    assert len(saved) == 1


def test_no_flush_before_time_interval(monkeypatch) -> None:
    engine = _fresh_engine()
    saved = []
    monkeypatch.setattr(engine, "_save_to_db", lambda: saved.append(1))
    engine._last_flush = time.monotonic() - (_INTERVAL - 60.0)
    engine.record_outcome("rule", "GREEN", "positive")
    assert saved == []


def test_counter_resets_after_successful_save(session, monkeypatch) -> None:
    """A successful real save resets the dirty counter and flush timer."""
    engine = _fresh_engine()
    _patch_db_session(monkeypatch, session)
    _record(engine, _THRESHOLD)
    assert engine._pending_outcomes == 0
    assert engine._last_flush <= time.monotonic()


def test_persist_pushes_state_to_db(session, monkeypatch) -> None:
    """End-to-end: after a flush, EnsembleState is DURABLY persisted.

    Read-back goes through a fresh session, so this only passes if the
    save was actually committed — a flush-then-close (no commit) would
    roll back and return None here.
    """
    engine = _fresh_engine()
    _patch_db_session(monkeypatch, session)
    _record(engine, _THRESHOLD)
    state = _read_state_fresh(session)
    assert state is not None
    assert state.total_predictions == _THRESHOLD
    rule_acc = state.scorer_accuracy["rule"]
    assert rule_acc["total_predictions"] == _THRESHOLD
    assert rule_acc["correct_predictions"] == _THRESHOLD  # GREEN + positive = correct


def test_failed_save_keeps_dirty_counter(monkeypatch) -> None:
    engine = _fresh_engine()
    monkeypatch.setattr(engine, "_save_to_db", lambda: False)
    _record(engine, _THRESHOLD)
    assert engine._pending_outcomes == _THRESHOLD  # not reset on failure


def test_failed_save_backoff_prevents_write_storm(monkeypatch) -> None:
    """A failing save retries at most once per backoff interval."""
    engine = _fresh_engine()
    attempts: list[int] = []

    def _failing_save() -> bool:
        attempts.append(1)
        return False

    monkeypatch.setattr(engine, "_save_to_db", _failing_save)
    _record(engine, _THRESHOLD)
    assert len(attempts) == 1  # threshold crossed → one attempt
    # Immediate follow-up outcomes must not re-attempt inside the backoff
    # window (counter stays pinned at threshold).
    _record(engine, 5)
    assert len(attempts) == 1


def test_second_flush_updates_sentinel_row_not_duplicate(session, monkeypatch) -> None:
    """Two flush cycles keep exactly one EnsembleState row.

    Guards the id=1 sentinel: the first flush creates the row, and a second
    threshold-crossing flush must UPDATE it rather than insert a duplicate
    (which two racing threads could otherwise both do).  All reads go
    through fresh sessions to prove committed durability.
    """
    engine = _fresh_engine()
    _patch_db_session(monkeypatch, session)
    _record(engine, _THRESHOLD)  # first flush → creates id=1 row
    first = _read_state_fresh(session)
    assert first is not None
    assert first.id == 1
    first_total = first.total_predictions

    _record(engine, _THRESHOLD)  # second flush → must update, not insert
    second = _read_state_fresh(session)
    assert second is not None
    assert second.id == 1
    assert second.total_predictions == first_total + _THRESHOLD
