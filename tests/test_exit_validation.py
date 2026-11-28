"""Mission-refactor Task 8: exit evaluator + ``exit_validated`` gate.

The exit mission is graded on captured fraction (spec §4): of the gain
that was actually executable at the peak (PEV, Task 7's 10%-reference
executable peak), how much did the exit policy keep — always in
proceeds terms, never spot price. The gate (spec §6) requires a strict
beat over the fixed-ladder benchmark on BOTH the median and the p25
captured fraction: a policy that helps typical trades and hurts bad
ones fails where the money is lost. Real ``ExitRow``s need the Task 9
policy, so until then ``load_exit_rows`` is an honest stub and the
gate reads False.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from validation.harness import (
    ExitRow,
    captured_fraction,
    evaluate_exit,
    load_exit_rows,
    regret_decomposition,
)
from validation.report import MetricCell
from validation.status import exit_validation_status

TS = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

EXIT_METRICS = {
    "captured_fraction",
    "median_captured_fraction",
    "p25_captured_fraction",
    "mean_total_regret",
    "horizon_captured_fraction",
}


def _cell(cells: list[MetricCell], output: str, metric: str) -> MetricCell:
    return next(c for c in cells if c.output == output and c.metric == metric)


def _row(
    asset_id: int,
    *,
    pev: float,
    clairvoyant: float,
    policy: float,
    ladder: float,
    horizon: float,
) -> ExitRow:
    return ExitRow(
        asset_id=asset_id,
        decision_ts=TS,
        pev_gain_pct=pev,
        clairvoyant_gain_pct=clairvoyant,
        policy_gain_pct=policy,
        ladder_gain_pct=ladder,
        horizon_gain_pct=horizon,
    )


# ── Captured fraction + regret decomposition (pure) ────────────────────────


def test_captured_fraction_edges():
    assert captured_fraction(1.0, 2.0) == pytest.approx(0.5)
    assert captured_fraction(0.0, 0.0) == 0.0
    assert captured_fraction(0.3, -0.1) == 1.0
    # Full capture and a losing policy against real executable upside.
    assert captured_fraction(2.0, 2.0) == pytest.approx(1.0)
    assert captured_fraction(-0.5, 2.0) == pytest.approx(-0.25)


def test_regret_decomposition_sums_to_total():
    row = ExitRow(
        asset_id=1,
        decision_ts=TS,
        pev_gain_pct=1.8,
        clairvoyant_gain_pct=2.0,
        policy_gain_pct=1.2,
        ladder_gain_pct=1.0,
        horizon_gain_pct=0.4,
    )
    parts = regret_decomposition(row)
    assert parts["executability_gap"] == pytest.approx(0.2)
    assert sum(parts.values()) == pytest.approx(2.0 - 1.2)


# ── Evaluator cells on synthetic episodes ──────────────────────────────────


def test_evaluate_exit_insufficient_samples():
    cells = evaluate_exit([], regime="live")
    assert {c.metric for c in cells} == EXIT_METRICS
    assert all(c.output == "exit_policy" for c in cells)
    assert all(c.verdict == "insufficient_data" for c in cells)

    few = [_row(i, pev=2.0, clairvoyant=2.2, policy=1.8, ladder=1.0, horizon=0.6) for i in range(5)]
    cells = evaluate_exit(few, regime="live", min_samples=30)
    assert all(c.verdict == "insufficient_data" for c in cells)


def test_evaluate_exit_policy_dominates_ladder():
    # 40 episodes, PEV gain 2.0 throughout: policy keeps CF 0.9, the
    # ladder CF 0.5, the 24h horizon CF 0.3 — constant fractions, so
    # mean / median / p25 coincide and every gate cell must beat.
    rows = [
        _row(i, pev=2.0, clairvoyant=2.2, policy=1.8, ladder=1.0, horizon=0.6) for i in range(40)
    ]
    cells = evaluate_exit(rows, regime="live")
    for metric in ("captured_fraction", "median_captured_fraction", "p25_captured_fraction"):
        cell = _cell(cells, "exit_policy", metric)
        assert cell.value == pytest.approx(0.9)
        assert cell.baseline_value == pytest.approx(0.5)
        assert cell.verdict == "better_than_baseline"
    horizon = _cell(cells, "exit_policy", "horizon_captured_fraction")
    assert horizon.value == pytest.approx(0.3)
    assert horizon.baseline_value == pytest.approx(0.5)
    regret = _cell(cells, "exit_policy", "mean_total_regret")
    assert regret.value == pytest.approx(2.2 - 1.8)
    assert regret.baseline_value == pytest.approx(2.2 - 1.0)


def test_evaluate_exit_median_beats_but_p25_loses():
    # The spec §6 failure shape: the policy's typical episode captures
    # far more than the ladder (median CF 0.9 vs 0.5, mean also ahead),
    # but its bad tail captures almost nothing (p25 CF 0.1 vs 0.5).
    rows: list[ExitRow] = []
    for i in range(16):
        rows.append(_row(100 + i, pev=2.0, clairvoyant=2.2, policy=0.2, ladder=1.0, horizon=0.6))
    for i in range(24):
        rows.append(_row(200 + i, pev=2.0, clairvoyant=2.2, policy=1.8, ladder=1.0, horizon=0.6))
    cells = evaluate_exit(rows, regime="live")
    median = _cell(cells, "exit_policy", "median_captured_fraction")
    assert median.value == pytest.approx(0.9)
    assert median.baseline_value == pytest.approx(0.5)
    mean = _cell(cells, "exit_policy", "captured_fraction")
    assert mean.value == pytest.approx(0.58)
    assert mean.baseline_value == pytest.approx(0.5)
    p25 = _cell(cells, "exit_policy", "p25_captured_fraction")
    assert p25.value == pytest.approx(0.1)
    assert p25.baseline_value == pytest.approx(0.5)
    # The point estimate is far below the ladder, but at n=40 the
    # bootstrap CI on a 40%-mass tail statistic still brushes the
    # baseline, so the verdict is "indistinguishable" rather than
    # outright "worse" — either way it is NOT a strict beat, which is
    # what the gate reads.
    assert p25.verdict in {"indistinguishable_from_baseline", "worse_than_baseline"}


def test_load_exit_rows_stub_is_empty(session):
    # Real rows need the Task 9 exit policy; until then the loader is
    # an honest stub — no fabricated episodes.
    assert load_exit_rows(session) == []


# ── Gate: strict beat on mean AND median AND p25 ───────────────────────────


def _report_json(cells: list[dict], generated_at: str = "2026-11-28T05:00:00+00:00") -> dict:
    return {"generated_at_utc": generated_at, "cells": cells}


def _json_cell(
    output: str, metric: str, value: float, baseline: float, verdict: str, n: int
) -> dict:
    return {
        "output": output,
        "metric": metric,
        "value": value,
        "baseline_value": baseline,
        "verdict": verdict,
        "n": n,
    }


def _write_report(tmp_path: Path, cells: list[dict], name: str = "validation-exit.json") -> Path:
    (tmp_path / name).write_text(json.dumps(_report_json(cells)), encoding="utf-8")
    return tmp_path


def _gate_cells(cf: str, median: str, p25: str) -> list[dict]:
    return [
        _json_cell("exit_policy", "captured_fraction", 0.80, 0.50, cf, 500),
        _json_cell("exit_policy", "median_captured_fraction", 0.85, 0.50, median, 500),
        _json_cell("exit_policy", "p25_captured_fraction", 0.60, 0.40, p25, 500),
    ]


def test_exit_gate_requires_strict_beat_over_ladder(tmp_path):
    # captured_fraction cell "indistinguishable" -> exit_validated False.
    _write_report(
        tmp_path,
        _gate_cells(
            "indistinguishable_from_baseline", "better_than_baseline", "better_than_baseline"
        ),
    )
    status = exit_validation_status(reports_dir=tmp_path)
    assert status["exit_validated"] is False
    assert "exit captured fraction vs ladder" in status["exit_validation_reason"]


def test_exit_gate_fails_when_median_beats_but_p25_does_not(tmp_path):
    # Mean and median captured-fraction cells beat the ladder, but the
    # p25 cell is "indistinguishable" -> exit_validated False, with the
    # p25 cell named in the reason. Spec strength: median AND p25.
    _write_report(
        tmp_path,
        _gate_cells(
            "better_than_baseline", "better_than_baseline", "indistinguishable_from_baseline"
        ),
    )
    status = exit_validation_status(reports_dir=tmp_path)
    assert status["exit_validated"] is False
    assert "exit p25 captured fraction vs ladder" in status["exit_validation_reason"]


def test_exit_gate_passes_when_all_cells_beat(tmp_path):
    _write_report(
        tmp_path,
        _gate_cells("better_than_baseline", "better_than_baseline", "better_than_baseline"),
    )
    status = exit_validation_status(reports_dir=tmp_path)
    assert status["exit_validated"] is True
    assert status["exit_validation_report"] == "validation-exit.json"


def test_exit_gate_missing_cell_fails(tmp_path):
    _write_report(
        tmp_path,
        _gate_cells("better_than_baseline", "better_than_baseline", "better_than_baseline")[:2],
    )
    status = exit_validation_status(reports_dir=tmp_path)
    assert status["exit_validated"] is False
    assert "exit p25 captured fraction vs ladder: missing" in status["exit_validation_reason"]


def test_exit_gate_no_report(tmp_path):
    status = exit_validation_status(reports_dir=tmp_path)
    assert status["exit_validated"] is False
    assert status["exit_validation_report"] is None


def test_exit_gate_end_to_end_median_win_p25_loss_fails(tmp_path):
    # Full path: synthetic episodes -> evaluate_exit cells -> report
    # JSON -> gate. The policy wins the median and loses the p25, so
    # the gate must read False with the p25 cell named.
    rows: list[ExitRow] = []
    for i in range(16):
        rows.append(_row(100 + i, pev=2.0, clairvoyant=2.2, policy=0.2, ladder=1.0, horizon=0.6))
    for i in range(24):
        rows.append(_row(200 + i, pev=2.0, clairvoyant=2.2, policy=1.8, ladder=1.0, horizon=0.6))
    cells = evaluate_exit(rows, regime="live")
    payload = _report_json([c.as_dict() for c in cells])
    (tmp_path / "validation-exit-e2e.json").write_text(json.dumps(payload), encoding="utf-8")
    status = exit_validation_status(reports_dir=tmp_path)
    assert status["exit_validated"] is False
    assert "exit p25 captured fraction vs ladder" in status["exit_validation_reason"]
