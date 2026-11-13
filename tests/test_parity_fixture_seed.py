from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select

from common.config import Settings
from common.time import ensure_utc
from ops.archive import run_archive
from ops.parity import require_compared, run_parity
from scripts.seed_fixtures import seed_parity_window
from storage import models


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        archive_enabled=True,
        archive_backend="local",
        archive_local_dir=str(tmp_path),
        archive_compact_after_hours=72.0,
        archive_retention_days=30,
        archive_batch_size=5_000,
        parity_enabled=True,
        parity_compare_hours_ago=96.0,
    )


def test_seeded_parity_window_archives_and_compares(session, tmp_path) -> None:
    """The CI fixture must feed both halves of the smoke: evidence old enough to
    be due for compaction, and observations at the decision hour both read paths
    compare with zero mismatches."""
    settings = _settings(tmp_path)
    summary = seed_parity_window(session=session, settings=settings)
    decision_ts = datetime.fromisoformat(str(summary["decision_ts"]))

    evidence_rows = session.scalar(select(func.count()).select_from(models.RawEvidenceItem))
    assert evidence_rows and evidence_rows >= 6
    observed = session.scalars(select(models.RawEvidenceItem.observed_at)).all()
    # SQLite hands back naive datetimes; the seeding is UTC throughout.
    assert max(ensure_utc(ts) for ts in observed) <= decision_ts

    archived = run_archive(session, settings=settings)
    session.commit()
    assert archived["partitions"] > 0, archived
    assert archived["compacted"] > 0, archived

    parity = run_parity(session, decision_ts=decision_ts, settings=settings)
    assert parity["status"] == "ok", parity
    assert parity["mismatches"] == 0, parity
    # The seeded base asset and its quote asset both compare clean.
    assert parity["compared_assets"] == 2, parity
    # The guard CI relies on accepts a real comparison and rejects an empty one.
    require_compared(parity)


def test_require_compared_rejects_a_vacuous_run() -> None:
    with pytest.raises(SystemExit):
        require_compared({"status": "ok", "mismatches": 0, "compared_assets": 0})
