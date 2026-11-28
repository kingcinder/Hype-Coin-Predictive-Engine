"""Mission-refactor Task 6: entry evaluator cells + ``entry_validated`` gate.

The harness has always scored the collapse mission; these tests pin the
entry side: ``p_ignition_24h`` calibration against Task 2's continuous
forward labels, ``research_priority`` top-group hit rates (gain lift is a
HIT-RATE ratio at the §5 gain threshold, never a mean-gain ratio), the
live-stratum strict-beat gate, and the forecast-freshness precondition
(stale forecasts fail the gate with a named reason instead of being
evaluated as if current).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from validation.harness import EntryRow, evaluate_entry
from validation.report import MetricCell
from validation.status import entry_validation_status

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _cell(cells: list[MetricCell], output: str, metric: str) -> MetricCell:
    return next(c for c in cells if c.output == output and c.metric == metric)


def _row(
    asset_id: int,
    *,
    priority: float | None = None,
    p_ignition: float | None = None,
    gain: float = 0.0,
    hours: float | None = None,
    phase: str | None = None,
    ts: datetime = T0,
) -> EntryRow:
    return EntryRow(
        asset_id=asset_id,
        decision_ts=ts,
        research_priority=priority,
        p_ignition_24h=p_ignition,
        max_gain_pct=gain,
        hours_to_peak=hours,
        phase_at_score=phase,
    )


# ── Gain lift: hit-rate ratio, not a mean ratio ─────────────────────────────


def test_gain_lift_is_hit_rate_ratio_not_mean_ratio() -> None:
    # 100 rows: top decile (10 rows by research_priority) has 4 rows at
    # max_gain_pct >= 1.0 (hit rate 0.40); all 100 rows have 10 hits
    # (base hit rate 0.10) -> gain_lift_top_decile == 4.0. One top-decile
    # row carries max_gain_pct = 50.0: under the old mean-ratio
    # definition that single runner would dominate the metric; the
    # hit-rate value must be exactly 4.0 regardless of its magnitude.
    rows: list[EntryRow] = []
    for i in range(1, 101):
        gain = 0.1
        if i > 90:  # top decile by priority: priorities 91..100
            gain = (50.0, 1.5, 2.0, 3.0)[i - 91] if i <= 94 else 0.1
        elif i <= 6:  # 6 more hits outside the top decile -> 10 total
            gain = 1.2
        rows.append(_row(i, priority=float(i), gain=gain))
    cells = evaluate_entry(rows, regime="live")
    lift = _cell(cells, "entry_priority", "gain_lift_top_decile")
    assert lift.value == pytest.approx(4.0)
    assert lift.baseline_value == pytest.approx(1.0)
    # The p_ignition block is structurally present even with no forecasts.
    brier = _cell(cells, "p_ignition_24h", "brier")
    assert brier.verdict == "insufficient_data"
    _cell(cells, "p_ignition_24h", "calibration_error")


def test_entry_live_cells_and_median_hours_to_peak() -> None:
    # 40 live rows, 40 dead rows; every row ignites (gain >= 0.20) but the
    # live cohort peaks fast (median 2h) and the dead cohort slow (10h).
    rows: list[EntryRow] = []
    for i in range(40):
        rows.append(
            _row(
                1000 + i,
                priority=float(i),
                p_ignition=0.6,
                gain=0.5,
                hours=2.0,
                phase="ignition",
            )
        )
    for i in range(40):
        rows.append(
            _row(
                2000 + i,
                priority=float(i),
                p_ignition=0.6,
                gain=0.5,
                hours=10.0,
                phase="collapse",
            )
        )
    cells = evaluate_entry(rows, regime="mixed", min_samples=30)
    live_precision = _cell(cells, "entry_priority:live", "precision_at_20")
    assert live_precision.n == 40
    median_cell = _cell(cells, "entry_priority:live", "median_hours_to_peak")
    assert median_cell.n == 40
    assert median_cell.value == pytest.approx(2.0)
    # Baseline is the median over ALL ignited rows, not the live subset.
    assert median_cell.baseline_value == pytest.approx(6.0)


# ── Gate: structural copy of the scores gate ────────────────────────────────


def _report_json(cells: list[dict], generated_at: str = "2026-11-28T04:15:39+00:00") -> dict:
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


def _write_report(tmp_path: Path, cells: list[dict], name: str = "validation-entry.json") -> Path:
    (tmp_path / name).write_text(json.dumps(_report_json(cells)), encoding="utf-8")
    return tmp_path


def _passing_cells(live_verdict: str) -> list[dict]:
    return [
        _json_cell("p_ignition_24h", "calibration_error", 0.05, 0.1, "better_than_baseline", 500),
        _json_cell("entry_priority:live", "precision_at_20", 0.30, 0.10, live_verdict, 500),
        _json_cell("entry_priority:live", "gain_lift_top_decile", 2.5, 1.0, live_verdict, 500),
    ]


def test_entry_gate_requires_live_strict_beat(tmp_path: Path) -> None:
    # Headline entry cells pass but entry_priority:live is merely
    # "indistinguishable" -> entry_validated is False with the live cell
    # named in the reason (mirrors the scores gate's live-edge rule).
    _write_report(tmp_path, _passing_cells("indistinguishable"))
    status = entry_validation_status(reports_dir=tmp_path)
    assert status["entry_validated"] is False
    assert "entry precision@20 (live tokens)" in status["entry_validation_reason"]


def test_entry_gate_passes_when_live_cells_beat(tmp_path: Path) -> None:
    _write_report(tmp_path, _passing_cells("beats_baseline"))
    status = entry_validation_status(reports_dir=tmp_path)
    assert status["entry_validated"] is True
    assert status["entry_validation_report"] == "validation-entry.json"


def test_freshness_cell_takes_precedence_in_status(tmp_path: Path) -> None:
    # A report carrying the freshness cell as insufficient_data fails the
    # gate with the stale-forecasts reason ahead of any per-cell list,
    # even when every entry cell would otherwise pass.
    cells = _passing_cells("beats_baseline") + [
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
    ]
    _write_report(tmp_path, cells)
    status = entry_validation_status(reports_dir=tmp_path)
    assert status["entry_validated"] is False
    assert status["entry_validation_reason"].startswith("stale forecasts")


def test_entry_gate_no_report(tmp_path: Path) -> None:
    status = entry_validation_status(reports_dir=tmp_path)
    assert status["entry_validated"] is False
    assert status["entry_validation_report"] is None


# ── Harness wiring: embargo split + freshness precondition ─────────────────


def _seed_score(session, asset_id: int, ts: datetime, *, priority: float, version: str) -> int:
    from storage import models

    score = models.Score(
        asset_id=asset_id,
        decision_ts=ts,
        observed_at=ts,
        hype=0.5,
        ethos=0.5,
        risk=50.0,
        liquidity_access=0.5,
        manipulation=0.1,
        confidence=0.8,
        uncertainty=0.2,
        catalyst=0.3,
        exit_risk=0.5,
        research_priority=priority,
        risk_band="YELLOW",
        model_version=version,
    )
    session.add(score)
    session.flush()
    return score.id


def _seed_outcome(session, asset_id: int, ts: datetime, *, suffix: str) -> None:
    from storage import models

    score_id = _seed_score(session, asset_id, ts, priority=0.5, version=f"test-entry-{suffix}")
    session.add(
        models.RiskOutcome(
            asset_id=asset_id,
            score_id=score_id,
            risk_band="YELLOW",
            scored_at=ts,
            lifecycle_phase_at_score="seeding",
            evaluated_at=ts,
            collapsed=False,
            rugged=False,
            survived=True,
            label_version="v3_bounded",
        )
    )


def _seed_entry_point(
    session,
    asset_id: int,
    ts: datetime,
    *,
    forecast_decision_ts: datetime,
    gain: float = 1.5,
) -> None:
    """One entry point: forward labels + a score + a forecast made from ts."""
    from storage import models

    session.add_all(
        [
            models.Label(
                asset_id=asset_id,
                ts=ts,
                observed_at=ts,
                label_type="forward_max_gain_pct",
                label_value=f"{gain:.6f}",
                label_source="test-entry",
            ),
            models.Label(
                asset_id=asset_id,
                ts=ts,
                observed_at=ts,
                label_type="hours_to_peak",
                label_value="3.000000",
                label_source="test-entry",
            ),
        ]
    )
    _seed_score(session, asset_id, ts, priority=0.9, version="test-entry-point")
    session.add(
        models.Forecast(
            asset_id=asset_id,
            decision_ts=forecast_decision_ts,
            observed_at=forecast_decision_ts,
            p_ignition_24h=0.6,
            p_collapse_24h=0.2,
            expected_hours_to_peak=3.0,
            expected_hours_to_collapse=None,
            details={"source_features_ts": ts.isoformat()},
            model_version="test-entry",
        )
    )


def _seed_asset(session, chain_id: int, suffix: str):
    from storage.repository import upsert_asset

    return upsert_asset(
        session,
        chain_id=chain_id,
        address=f"Entry{suffix}1111111111111111111111111111111",
        symbol=f"E{suffix[:2]}",
        name=f"Entry {suffix} Fixture",
        first_seen_at=T0,
    )


def test_entry_cells_respect_embargo_split(session) -> None:
    # Outcomes span day 0..day 40, so the walk-forward cutoff lands at
    # day 28 and the 48h embargo covers (day 26, day 28]. Entry points in
    # the reference window (day 1) and the embargo (day 27) must appear in
    # NEITHER the eval entry cells nor any baseline they feed — only the
    # day-39 eval point (window C) is counted.
    from tests.conftest import seed_reference
    from validation.harness import run_harness

    chain, _source = seed_reference(session)
    now = datetime.now(UTC)

    anchor_a = _seed_asset(session, chain.id, "AnchorA")
    anchor_b = _seed_asset(session, chain.id, "AnchorB")
    _seed_outcome(session, anchor_a.id, T0, suffix="a")
    _seed_outcome(session, anchor_b.id, T0 + timedelta(days=40), suffix="b")

    for suffix, ts in (
        ("Ref", T0 + timedelta(days=1)),
        ("Emb", T0 + timedelta(days=27)),
        ("Eval", T0 + timedelta(days=39)),
    ):
        asset = _seed_asset(session, chain.id, suffix)
        _seed_entry_point(session, asset.id, ts, forecast_decision_ts=now)
    session.commit()

    report = run_harness(session, min_samples=1)
    by_key = {(c.output, c.metric): c for c in report.cells}

    assert ("forecasts", "freshness") not in by_key
    precision = by_key[("entry_priority", "precision_at_20")]
    assert precision.n == 1  # window C only: reference + embargo excluded
    lift = by_key[("entry_priority", "gain_lift_top_decile")]
    assert lift.n == 1
    brier = by_key[("p_ignition_24h", "brier")]
    assert brier.n == 1


def test_stale_forecasts_fail_entry_gate_with_named_reason(session, tmp_path: Path) -> None:
    # Newest forecasts.decision_ts is 48h old with
    # forecast_max_staleness_hours = 6: no entry cells are computed from
    # the stale rows, and the status reads False, reason starting with
    # "stale forecasts".
    from tests.conftest import seed_reference
    from validation.harness import run_harness

    chain, _source = seed_reference(session)
    asset = _seed_asset(session, chain.id, "Stale")
    stale_ts = datetime.now(UTC) - timedelta(hours=48)
    _seed_entry_point(session, asset.id, T0, forecast_decision_ts=stale_ts)
    session.commit()

    report = run_harness(session, min_samples=1)
    by_key = {(c.output, c.metric): c for c in report.cells}

    freshness = by_key[("forecasts", "freshness")]
    assert freshness.verdict == "insufficient_data"
    assert freshness.note.startswith("stale forecasts")
    assert not any(output.startswith("entry_priority") for output, _metric in by_key)
    assert ("p_ignition_24h", "brier") not in by_key

    report.save(tmp_path / "validation-stale.json")
    status = entry_validation_status(reports_dir=tmp_path)
    assert status["entry_validated"] is False
    assert status["entry_validation_reason"].startswith("stale forecasts")
