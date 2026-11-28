"""Trading payloads must carry the two mission verdicts (Task 11).

``scores_validated`` is retired: the surface reads ``entry_validated`` /
``exit_validated`` from :func:`validation.status.mission_validation_status`,
each with its own reason and report name.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from api import trading_routes
from common.config import get_settings
from storage.write_queue import start_write_queue, stop_write_queue
from validation.status import mission_validation_status


@pytest.fixture
def queue(session):
    factory = sessionmaker(bind=session.bind, autoflush=False, expire_on_commit=False)
    start_write_queue(session_factory=factory)
    yield
    stop_write_queue()


def _report(cells: list[dict], generated_at: str = "2026-11-14T04:15:39+00:00") -> dict:
    return {"generated_at_utc": generated_at, "cells": cells}


def _cell(output: str, metric: str, value: float, baseline: float, verdict: str, n: int) -> dict:
    return {
        "output": output,
        "metric": metric,
        "value": value,
        "baseline_value": baseline,
        "verdict": verdict,
        "n": n,
    }


def _passing_entry_cells() -> list[dict]:
    return [
        _cell("p_ignition_24h", "calibration_error", 0.05, 0.1, "better_than_baseline", 500),
        _cell("entry_priority:live", "precision_at_20", 0.30, 0.10, "beats_baseline", 500),
        _cell("entry_priority:live", "gain_lift_top_decile", 2.5, 1.0, "beats_baseline", 500),
    ]


def _passing_exit_cells() -> list[dict]:
    return [
        _cell("exit_policy", "captured_fraction", 0.80, 0.50, "beats_baseline", 500),
        _cell("exit_policy", "median_captured_fraction", 0.85, 0.50, "beats_baseline", 500),
        _cell("exit_policy", "p25_captured_fraction", 0.60, 0.40, "beats_baseline", 500),
    ]


def _failing_dir(tmp_path: Path) -> Path:
    """A reports dir whose newest report fails the entry gate (live cells
    merely indistinguishable) and carries no exit cells at all."""
    (tmp_path / "validation-failing.json").write_text(
        json.dumps(
            _report(
                [
                    _cell(
                        "p_ignition_24h",
                        "calibration_error",
                        0.464,
                        0.1,
                        "worse_than_baseline",
                        306,
                    ),
                    _cell(
                        "entry_priority:live",
                        "precision_at_20",
                        0.027,
                        0.028,
                        "indistinguishable",
                        298,
                    ),
                    _cell(
                        "entry_priority:live",
                        "gain_lift_top_decile",
                        1.0,
                        1.0,
                        "indistinguishable",
                        298,
                    ),
                ]
            )
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_failed_report_yields_false_with_reasons(tmp_path: Path) -> None:
    status = mission_validation_status(_failing_dir(tmp_path))

    assert status["entry_validated"] is False
    assert "not validated" in status["entry_validation_reason"]
    assert "live tokens" in status["entry_validation_reason"]
    assert "indistinguishable" in status["entry_validation_reason"]
    assert status["entry_validation_report"] == "validation-failing.json"
    assert status["exit_validated"] is False
    assert "missing" in status["exit_validation_reason"]
    assert "scores_validated" not in status


def test_passing_report_yields_true_for_both_gates(tmp_path: Path) -> None:
    (tmp_path / "validation-b.json").write_text(
        json.dumps(_report(_passing_entry_cells() + _passing_exit_cells())),
        encoding="utf-8",
    )

    status = mission_validation_status(tmp_path)

    assert status["entry_validated"] is True
    assert status["exit_validated"] is True
    assert status["entry_validation_report"] == "validation-b.json"
    assert status["exit_validation_report"] == "validation-b.json"


def test_stale_forecasts_fail_entry_through_the_merge(tmp_path: Path) -> None:
    """The freshness precondition survives the merge: a report whose entry
    cells pass but whose forecasts were stale reads entry_validated False
    with the stale-forecasts reason."""
    cells = _passing_entry_cells() + _passing_exit_cells()
    cells.append(
        {
            "output": "forecasts",
            "metric": "freshness",
            "value": None,
            "baseline_value": None,
            "verdict": "insufficient_data",
            "n": 12,
            "note": (
                "stale forecasts: newest forecast decision_ts "
                "2026-11-27T09:18:00+00:00 is older than forecast_max_staleness_hours"
            ),
        }
    )
    (tmp_path / "validation-stale.json").write_text(json.dumps(_report(cells)), encoding="utf-8")

    status = mission_validation_status(tmp_path)

    assert status["entry_validated"] is False
    assert status["entry_validation_reason"].startswith("stale forecasts")
    assert status["exit_validated"] is True


def test_no_report_yields_false_for_both_gates(tmp_path: Path) -> None:
    status = mission_validation_status(tmp_path)

    assert status["entry_validated"] is False
    assert status["exit_validated"] is False
    assert status["entry_validation_report"] is None
    assert status["exit_validation_report"] is None


def test_trading_status_and_candidates_carry_the_flags(
    session, queue, tmp_path: Path, monkeypatch
) -> None:
    # Isolate from the real reports/ dir: a passing report legitimately
    # flips the flags (validation/status.py), so pin a failing report here.
    monkeypatch.setattr("validation.status.REPORTS_DIR", _failing_dir(tmp_path))
    status = asyncio.run(trading_routes.trading_status(session))
    assert status["entry_validated"] is False
    assert status["exit_validated"] is False
    assert status["entry_validation_reason"]
    assert status["exit_validation_reason"]
    assert "scores_validated" not in status

    candidates = asyncio.run(trading_routes.paper_candidates(session, limit=3))
    assert candidates["entry_validated"] is False
    assert candidates["exit_validated"] is False
    assert "scores_validated" not in candidates


def test_arm_response_carries_the_flags(session, queue, monkeypatch, tmp_path: Path) -> None:
    from trading.live_gate import ARM_PHRASE

    monkeypatch.setattr("validation.status.REPORTS_DIR", _failing_dir(tmp_path))
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(
        trading_routes,
        "utc_now",
        lambda: datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )

    body = trading_routes.ArmBody(confirm=ARM_PHRASE)
    result = asyncio.run(trading_routes.trading_arm(body, session))

    assert result["status"] == "armed"
    assert result["entry_validated"] is False
    assert result["exit_validated"] is False
    assert "scores_validated" not in result
    get_settings.cache_clear()


def test_copy_signal_payload_carries_the_flags(session, queue, monkeypatch, tmp_path: Path) -> None:
    from decimal import Decimal

    monkeypatch.setattr("validation.status.REPORTS_DIR", _failing_dir(tmp_path))

    class Row:
        status = "pending"
        id = 7
        detail = "awaiting human approval"

    monkeypatch.setattr("trading.copy.maybe_queue_copy", lambda *a, **k: Row())
    body = trading_routes.CopySignalBody(wallet="wallet", mint="mint", size_sol=Decimal("1"))

    result = asyncio.run(trading_routes.trading_copy_signal(body, session))

    assert result["status"] == "pending"
    assert result["entry_validated"] is False
    assert result["exit_validated"] is False
    assert result["entry_validation_reason"]
    assert result["exit_validation_reason"]
    assert "scores_validated" not in result


def test_ui_trading_view_exists() -> None:
    from ui.trading_view import trading_view

    assert callable(trading_view)
