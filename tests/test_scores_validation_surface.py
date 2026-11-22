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


def _live_edge_cell(
    value: float = 0.027, baseline: float = 0.028, verdict: str = "indistinguishable", n: int = 298
) -> dict:
    """The live-token BLACK cell (stratified output ``risk_band:live:BLACK``)."""
    return _cell("risk_band:live:BLACK", "collapse_precision", value, baseline, verdict, n)


def _failing_dir(tmp_path: Path) -> Path:
    """A reports dir whose newest report fails all four required cells."""
    (tmp_path / "validation-failing.json").write_text(
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
                    _live_edge_cell(),
                ]
            )
        ),
        encoding="utf-8",
    )
    return tmp_path


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
                    _live_edge_cell(),
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
                    _live_edge_cell(value=0.05, baseline=0.02, verdict="beats_baseline", n=120),
                ]
            )
        ),
        encoding="utf-8",
    )

    assert scores_validation_status(tmp_path)["scores_validated"] is True


def test_headline_pass_without_live_edge_yields_false(tmp_path: Path) -> None:
    """The new criterion: headline cells passing on dead-token structure alone
    must not validate the scores. The live-token BLACK cell below is the real
    v6 stratified read (0.0268 vs a 0.0217 live base rate, indistinguishable)."""
    (tmp_path / "validation-c.json").write_text(
        json.dumps(
            _report(
                [
                    _cell("risk_band", "ordinal_distance", 0.94, 3.29, "beats_baseline", 27468),
                    _cell("confidence", "calibration_error", 0.01, 0.1, "beats_baseline", 27468),
                    _cell(
                        "risk_band:BLACK",
                        "collapse_precision",
                        0.87,
                        0.82,
                        "beats_baseline",
                        20510,
                    ),
                    _live_edge_cell(
                        value=0.0268,
                        baseline=0.0217,
                        verdict="indistinguishable",
                        n=2835,
                    ),
                ]
            )
        ),
        encoding="utf-8",
    )

    status = scores_validation_status(tmp_path)

    assert status["scores_validated"] is False
    assert "live tokens" in status["scores_validation_reason"]
    assert "indistinguishable" in status["scores_validation_reason"]


def test_missing_live_edge_cell_yields_false(tmp_path: Path) -> None:
    """A report from before live-token stratification has no live cell at all;
    the flag must stay false rather than grandfathering it in."""
    (tmp_path / "validation-d.json").write_text(
        json.dumps(
            _report(
                [
                    _cell("risk_band", "ordinal_distance", 0.94, 3.29, "beats_baseline", 27468),
                    _cell("confidence", "calibration_error", 0.01, 0.1, "beats_baseline", 27468),
                    _cell(
                        "risk_band:BLACK",
                        "collapse_precision",
                        0.87,
                        0.82,
                        "beats_baseline",
                        20510,
                    ),
                ]
            )
        ),
        encoding="utf-8",
    )

    status = scores_validation_status(tmp_path)

    assert status["scores_validated"] is False
    assert "BLACK collapse precision (live tokens): missing" in status["scores_validation_reason"]


def test_live_edge_better_than_baseline_verdict_passes(tmp_path: Path) -> None:
    """The strict live-edge check accepts either beating verdict string."""
    (tmp_path / "validation-e.json").write_text(
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
                    _live_edge_cell(
                        value=0.05, baseline=0.02, verdict="better_than_baseline", n=120
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
        _live_edge_cell(),
    ]
    new = [
        _cell("risk_band", "ordinal_distance", 1.0, 1.12, "better_than_baseline", 400),
        _cell("confidence", "calibration_error", 0.05, 0.1, "better_than_baseline", 400),
        _cell("risk_band:BLACK", "collapse_precision", 0.5, 0.28, "better_than_baseline", 120),
        _live_edge_cell(value=0.05, baseline=0.02, verdict="beats_baseline", n=120),
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


def test_trading_status_and_candidates_carry_the_flag(
    session, queue, tmp_path: Path, monkeypatch
) -> None:
    # Isolate from the real reports/ dir: a passing report legitimately
    # flips the flag (validation/status.py), so pin a failing report here.
    monkeypatch.setattr("validation.status.REPORTS_DIR", _failing_dir(tmp_path))
    status = asyncio.run(trading_routes.trading_status(session))
    assert status["scores_validated"] is False
    assert status["scores_validation_reason"]

    candidates = asyncio.run(trading_routes.paper_candidates(session, limit=3))
    assert candidates["scores_validated"] is False


def test_arm_response_carries_the_flag(session, queue, monkeypatch, tmp_path: Path) -> None:
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
    assert result["scores_validated"] is False
    get_settings.cache_clear()


def test_copy_signal_payload_carries_the_flag(session, queue, monkeypatch, tmp_path: Path) -> None:
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
    assert result["scores_validated"] is False
    assert result["scores_validation_reason"]


def test_ui_trading_view_exists() -> None:
    from ui.trading_view import trading_view

    assert callable(trading_view)
