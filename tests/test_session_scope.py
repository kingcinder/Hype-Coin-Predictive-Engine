from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import storage.database as dbmod
from common.config import Settings
from common.time import utc_now
from storage import models
from storage.database import Base, session_scope


class _LogRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))


@pytest.fixture()
def _scoped(tmp_path, monkeypatch):
    db_path = tmp_path / "scope.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", maker)
    monkeypatch.setattr(dbmod, "get_settings", lambda: Settings(session_scope_budget_seconds=1.0))
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    recorder = _LogRecorder()
    monkeypatch.setattr(dbmod, "log", recorder)
    return clock, recorder


def test_default_budget_is_ten_seconds() -> None:
    assert Settings().session_scope_budget_seconds == 10.0


def test_over_budget_flush_warns_with_structured_fields(_scoped) -> None:
    clock, recorder = _scoped
    with session_scope() as session:
        session.add(models.SystemHealth(component="test", state="ok", message="x", ts=utc_now()))
        session.flush()
        clock[0] += 2.0
    assert len(recorder.calls) == 1
    event, kw = recorder.calls[0]
    assert event == "session_scope_over_budget"
    assert kw["elapsed_sec"] == pytest.approx(2.0)
    assert kw["budget_sec"] == 1.0
    assert kw["measured_from"] == "first_flush"
    assert kw["had_flush"] is True


def test_under_budget_is_silent(_scoped) -> None:
    clock, recorder = _scoped
    with session_scope() as session:
        session.add(models.SystemHealth(component="test", state="ok", message="x", ts=utc_now()))
        session.flush()
        clock[0] += 0.5
    assert recorder.calls == []


def test_budget_check_runs_when_body_raises(_scoped) -> None:
    clock, recorder = _scoped
    with pytest.raises(RuntimeError):
        with session_scope() as session:
            session.add(
                models.SystemHealth(component="test", state="ok", message="x", ts=utc_now())
            )
            session.flush()
            clock[0] += 2.0
            raise RuntimeError("boom")
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == "session_scope_over_budget"
