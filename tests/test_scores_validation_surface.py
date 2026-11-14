"""Trading payloads must carry the scores-validated verdict (B2)."""

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
from validation.status import scores_validation_status


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


def test_failed_report_yields_false_with_reason(tmp_path: Path) -> None:
    (tmp_path / "validation-a.json").write_text(
        json.dumps(
            _report(
                [
                    _cell("risk_band", "ordinal_distance", 2.9, 1.12, "worse_than_baseline", 306),
                    _cell(
                        "confidence",
                        "calibration_error",
                        0.464,
                        0.1,
                        "worse_than_baseline",
                        306,
                    ),
                    _cell(
                        "risk_band:BLACK",
                        "collapse_precision",
                        0.27,
                        0.28,
                        "indistinguishable_from_baseline",
                        298,
                    ),
                ]
            )
        ),
        encoding="utf-8",
    )

    status = scores_validation_status(tmp_path)

    assert status["scores_validated"] is False
    assert "not validated" in status["scores_validation_reason"]
    assert "n=306" in status["scores_validation_reason"]
    assert status["scores_validation_report"] == "validation-a.json"


def test_passing_report_yields_true(tmp_path: Path) -> None:
    (tmp_path / "validation-b.json").write_text(
        json.dumps(
            _report(
                [
                    _cell("risk_band", "ordinal_distance", 1.0, 1.12, "better_than_baseline", 400),
                    _cell(
                        "confidence", "calibration_error", 0.05, 0.1, "better_than_baseline", 400
                    ),
                    _cell(
                        "risk_band:BLACK",
                        "collapse_precision",
                        0.5,
                        0.28,
                        "better_than_baseline",
                        120,
                    ),
                ]
            )
        ),
        encoding="utf-8",
    )

    assert scores_validation_status(tmp_path)["scores_validated"] is True


def test_newest_report_wins_by_timestamp(tmp_path: Path) -> None:
    old = [
        _cell("risk_band", "ordinal_distance", 2.9, 1.12, "worse_than_baseline", 306),
        _cell("confidence", "calibration_error", 0.46, 0.1, "worse_than_baseline", 306),
        _cell(
            "risk_band:BLACK",
            "collapse_precision",
            0.27,
            0.28,
            "indistinguishable_from_baseline",
            298,
        ),
    ]
    new = [
        _cell("risk_band", "ordinal_distance", 1.0, 1.12, "better_than_baseline", 400),
        _cell("confidence", "calibration_error", 0.05, 0.1, "better_than_baseline", 400),
        _cell("risk_band:BLACK", "collapse_precision", 0.5, 0.28, "better_than_baseline", 120),
    ]
    (tmp_path / "validation-2026-11-14T034123.000000_0000.json").write_text(
        json.dumps(_report(old, "2026-11-14T03:41:23+00:00")), encoding="utf-8"
    )
    (tmp_path / "validation-2026-11-14-v2-relabel.json").write_text(
        json.dumps(_report(new, "2026-11-14T04:15:39+00:00")), encoding="utf-8"
    )

    status = scores_validation_status(tmp_path)

    assert status["scores_validated"] is True
    assert status["scores_validation_report"] == "validation-2026-11-14-v2-relabel.json"


def test_trading_status_and_candidates_carry_the_flag(session, queue) -> None:
    status = asyncio.run(trading_routes.trading_status(session))
    assert status["scores_validated"] is False
    assert status["scores_validation_reason"]

    candidates = asyncio.run(trading_routes.paper_candidates(session, limit=3))
    assert candidates["scores_validated"] is False


def test_arm_response_carries_the_flag(session, queue, monkeypatch) -> None:
    from trading.live_gate import ARM_PHRASE

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
    assert result["scores_validated"] is False
    get_settings.cache_clear()


def test_copy_signal_payload_carries_the_flag(session, queue, monkeypatch) -> None:
    from decimal import Decimal

    class Row:
        status = "pending"
        id = 7
        detail = "awaiting human approval"

    monkeypatch.setattr("trading.copy.maybe_queue_copy", lambda *a, **k: Row())
    body = trading_routes.CopySignalBody(wallet="wallet", mint="mint", size_sol=Decimal("1"))

    result = asyncio.run(trading_routes.trading_copy_signal(body, session))

    assert result["status"] == "pending"
    assert result["scores_validated"] is False
    assert result["scores_validation_reason"]


def test_ui_trading_view_exists() -> None:
    from ui.trading_view import trading_view

    assert callable(trading_view)
