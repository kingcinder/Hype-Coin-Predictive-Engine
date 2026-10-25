"""Regression tests for the data-lake pass on SQLite.

Covers two bugs that made every real pass fail: naive datetimes read back
from SQLite were subtracted from aware ones, and ``run_data_lake_pass`` read
label progress keys from the wrong dict.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from data_lake.manager import run_data_lake_pass
from data_lake.signal import _temporal_score
from scripts.seed_fixtures import seed_fixture_data

DECISION_TS = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_temporal_score_accepts_naive_observed_at() -> None:
    """A naive value read back from SQLite must not crash the subtraction."""
    naive_observed = datetime(2026, 5, 1, 11, 0)  # naive, meaning UTC
    score, reasons = _temporal_score(naive_observed, DECISION_TS)

    assert score == 1.0
    assert "1.0h old" in reasons[0]


def test_temporal_score_matches_aware_and_naive_inputs() -> None:
    aware = _temporal_score(DECISION_TS - timedelta(hours=10), DECISION_TS)
    naive = _temporal_score((DECISION_TS - timedelta(hours=10)).replace(tzinfo=None), DECISION_TS)
    assert aware == naive


def test_data_lake_pass_succeeds_on_seeded_sqlite(session: Session) -> None:
    seed_fixture_data(session=session)

    result = run_data_lake_pass(session, decision_ts=DECISION_TS)

    assert "error" not in result, result.get("error")
    progress = result["progress"]
    assert "progress_pct" in progress
    assert "total_labels" in progress
    assert "min_samples_required" in progress
