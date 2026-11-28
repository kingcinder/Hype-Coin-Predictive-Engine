"""Mission status surface: two gates (entry/exit), never one (Task 11).

``scores_validated`` is retired. Every trading-facing surface — the
``/trading/status`` and ``/paper/candidates`` payloads and the Streamlit
banner — must carry ``entry_validated`` / ``exit_validated`` with their
own reasons, and the old keys must not appear anywhere.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from api import trading_routes
from storage.write_queue import start_write_queue, stop_write_queue
from validation.status import mission_validation_status


@pytest.fixture
def queue(session):
    factory = sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)
    start_write_queue(session_factory=factory)
    yield
    stop_write_queue()


def test_mission_status_merges_entry_and_exit(tmp_path: Path) -> None:
    status = mission_validation_status(tmp_path)

    assert status["entry_validated"] is False
    assert status["exit_validated"] is False
    assert status["entry_validation_reason"]
    assert status["exit_validation_reason"]
    assert "scores_validated" not in status
    assert "scores_validation_reason" not in status
    assert "scores_validation_report" not in status


def test_trading_status_carries_two_flags_not_one(
    session, queue, tmp_path: Path, monkeypatch
) -> None:
    # Empty reports dir: both gates honestly read False.
    monkeypatch.setattr("validation.status.REPORTS_DIR", tmp_path)

    status = asyncio.run(trading_routes.trading_status(session))

    assert status["entry_validated"] is False
    assert status["exit_validated"] is False
    assert status["entry_validation_reason"]
    assert status["exit_validation_reason"]
    assert "scores_validated" not in status
    assert "scores_validation_reason" not in status


def test_paper_candidates_carry_two_flags_not_one(
    session, queue, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("validation.status.REPORTS_DIR", tmp_path)

    candidates = asyncio.run(trading_routes.paper_candidates(session, limit=3))

    assert candidates["entry_validated"] is False
    assert candidates["exit_validated"] is False
    assert "scores_validated" not in candidates
    assert "scores_validation_reason" not in candidates


def test_ui_banner_names_entry_and_exit_separately() -> None:
    import ui.trading_view as trading_view

    source = inspect.getsource(trading_view)

    assert "entry_validation_reason" in source
    assert "exit_validation_reason" in source
    assert "entry_validated" in source
    assert "exit_validated" in source
    assert "scores_validated" not in source
    assert "scores_validation_reason" not in source
