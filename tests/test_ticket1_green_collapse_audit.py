from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.ticket1_green_collapse_audit import (
    LifecycleMark,
    Prediction,
    Snapshot,
    evaluate_prediction,
    main,
    parse_timestamp,
    percentile,
    run_audit,
)

T0 = datetime(2026, 11, 20, tzinfo=UTC)
_FMT = "%Y-%m-%d %H:%M:%S.%f"


def _ts(value: datetime) -> str:
    return value.strftime(_FMT)


def _build_fixture_db(path: Path) -> None:
    """Tiny synthetic DB: one GREEN-then-collapsed token, one survivor, one
    already-dead token, one censored token (no forward evidence), one RED."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE pairs (
                id INTEGER PRIMARY KEY, base_asset_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE market_snapshots (
                pair_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
                ts TEXT NOT NULL, observed_at TEXT NOT NULL,
                price_usd REAL, volume_usd REAL, buys INTEGER, sells INTEGER
            );
            CREATE TABLE lifecycle_events (
                asset_id INTEGER NOT NULL, phase TEXT NOT NULL,
                ts TEXT NOT NULL, observed_at TEXT NOT NULL
            );
            CREATE TABLE prediction_records (
                asset_id INTEGER NOT NULL, scored_at TEXT NOT NULL,
                risk_band TEXT NOT NULL
            );
            """
        )
        old = _ts(T0 - timedelta(days=10))
        for pair_id, asset_id in [(10, 1), (20, 2), (30, 3), (40, 4), (50, 5)]:
            connection.execute(
                "INSERT INTO pairs VALUES (?, ?, ?)", (pair_id, asset_id, old)
            )
        lifecycle = [
            (1, "seeding", T0 - timedelta(hours=2)),
            (2, "ignition", T0 - timedelta(hours=3)),
            (3, "collapse", T0 - timedelta(hours=1)),
            (4, "saturation", T0 - timedelta(hours=4)),
            (5, "parabolic", T0 - timedelta(hours=5)),
        ]
        for asset_id, phase, ts in lifecycle:
            connection.execute(
                "INSERT INTO lifecycle_events VALUES (?, ?, ?, ?)",
                (asset_id, phase, _ts(ts), _ts(ts)),
            )
        snapshots = [
            # Asset 1: calm 100 at T0-1h, trough 40 at T0+10h -> collapsed.
            (10, T0 - timedelta(hours=1), 100.0),
            (10, T0 + timedelta(hours=10), 40.0),
            # Asset 2: 100, drifts to 90 over 47h -> no collapse.
            (20, T0 - timedelta(minutes=30), 100.0),
            (20, T0 + timedelta(hours=5), 95.0),
            (20, T0 + timedelta(hours=47), 90.0),
            # Asset 3: already collapsed before the score.
            (30, T0 - timedelta(hours=2), 10.0),
            # Asset 4: last snapshot 30h before the score, nothing after.
            (40, T0 - timedelta(hours=30), 50.0),
            # Asset 5: RED prediction; forward dip is irrelevant.
            (50, T0 - timedelta(hours=1), 200.0),
            (50, T0 + timedelta(hours=20), 150.0),
        ]
        for pair_id, ts, price in snapshots:
            connection.execute(
                "INSERT INTO market_snapshots VALUES (?, 1, ?, ?, ?, 1000.0, 10, 5)",
                (pair_id, _ts(ts), _ts(ts), price),
            )
        predictions = [
            (1, T0, "GREEN"),
            (1, T0 + timedelta(hours=2), "GREEN"),
            (2, T0, "GREEN"),
            (3, T0, "GREEN"),
            (4, T0, "GREEN"),
            (5, T0, "RED"),
        ]
        for asset_id, scored_at, band in predictions:
            connection.execute(
                "INSERT INTO prediction_records VALUES (?, ?, ?)",
                (asset_id, _ts(scored_at), band),
            )
        connection.commit()
    finally:
        connection.close()


def test_parse_timestamp_variants() -> None:
    expected = datetime(2026, 11, 20, 1, 2, 3, tzinfo=UTC)
    assert parse_timestamp("2026-11-20 01:02:03.000000") == expected
    assert parse_timestamp("2026-11-20T01:02:03Z") == expected
    assert parse_timestamp("2026-11-20T01:02:03+00:00") == expected
    assert parse_timestamp(expected) == expected
    assert parse_timestamp(0) == datetime(1970, 1, 1, tzinfo=UTC)


def test_percentile_linear_interpolation() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([5.0], 99) == 5.0
    assert percentile([0.5, 1.0, 1.0, 2.0, 3.0, 30.0], 50) == 1.5
    assert percentile([0.5, 1.0, 1.0, 2.0, 3.0, 30.0], 100) == 30.0


def test_evaluate_prediction_pair_created_after_score_is_not_eligible() -> None:
    prediction = Prediction(asset_id=9, scored_at=T0, risk_band="GREEN")
    snapshots = [
        Snapshot(
            pair_id=90,
            source_id=1,
            ts=T0 - timedelta(hours=1),
            observed_at=T0 - timedelta(hours=1),
            price_usd=10.0,
            volume_usd=100.0,
            buys=1.0,
            sells=1.0,
        )
    ]
    audit = evaluate_prediction(
        prediction,
        snapshots,
        {90: T0 + timedelta(hours=1)},  # pair did not exist at score time
        [LifecycleMark(phase="seeding", ts=T0 - timedelta(hours=1),
                       observed_at=T0 - timedelta(hours=1))],
        window_hours=48.0,
        drawdown=0.50,
    )
    assert audit.live is True
    assert audit.snapshot_age_hours is None
    assert audit.evaluable is False
    assert audit.censor_reason == "no_entry_price"


def test_run_audit_fixture_baseline(tmp_path: Path) -> None:
    db_path = tmp_path / "fixture.db"
    _build_fixture_db(db_path)

    summary = run_audit(
        db_path, days=14, window_hours=48.0, drawdown=0.50, until=T0 + timedelta(hours=2)
    )

    assert summary["predictions_total"] == 6
    assert summary["green_total"] == 5
    # Asset 3 was already in collapse at score time -> not live.
    assert summary["green_live"] == 4
    # Asset 4 has no forward snapshot -> censored, not counted either way.
    assert summary["green_live_no_forward_evidence"] == 1
    assert summary["green_live_evaluable"] == 3
    assert summary["green_then_collapsed"] == 2
    assert summary["green_then_collapsed_rate"] == 2 / 3

    age_blocks = summary["snapshot_age_hours"]
    assert isinstance(age_blocks, dict)
    overall = age_blocks["overall"]
    assert overall["n"] == 6
    assert overall["p50"] == 1.5
    assert overall["max"] == 30.0
    collapsed = age_blocks["green_then_collapsed"]
    assert collapsed["n"] == 2
    assert collapsed["p50"] == 2.0
    assert collapsed["max"] == 3.0


def test_main_prints_summary_and_missing_db_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "fixture.db"
    _build_fixture_db(db_path)

    assert main([str(db_path), "--until", "2026-11-20T02:00:00Z"]) == 0
    out = capsys.readouterr().out
    assert "green_then_collapsed: 2" in out
    assert "snapshot_age_hours[green_then_collapsed]" in out

    assert main([str(tmp_path / "missing.db")]) == 2
