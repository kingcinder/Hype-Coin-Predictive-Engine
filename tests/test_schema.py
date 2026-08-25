from __future__ import annotations

from storage.database import Base


def test_required_tables_are_registered() -> None:
    required = {
        "chains",
        "assets",
        "contracts",
        "pairs",
        "pools",
        "market_snapshots",
        "liquidity_snapshots",
        "holders",
        "wallet_clusters",
        "contract_flags",
        "social_mentions",
        "news_items",
        "catalysts",
        "features",
        "scores",
        "alerts",
        "labels",
        "system_health",
        "sources",
        "raw_evidence_items",
        "wallet_cluster_members",
        "ingestion_watermarks",
        "score_explanations",
        "backtest_runs",
        "backtest_results",
        "ignition_events",
        "fingerprint_assessments",
        "prelaunch_candidates",
        "forecasts",
        "narrative_clusters",
        "archive_manifests",
        "lifecycle_events",
        "retention_runs",
        "rpc_pool_snapshots",
        "liquidity_removal_events",
    }
    assert required.issubset(Base.metadata.tables.keys())


def test_scores_keep_all_ten_score_channels() -> None:
    columns = set(Base.metadata.tables["scores"].columns.keys())
    assert {
        "hype",
        "ethos",
        "risk",
        "liquidity_access",
        "manipulation",
        "confidence",
        "uncertainty",
        "catalyst",
        "exit_risk",
        "research_priority",
    }.issubset(columns)


def test_migration_chain_builds_cleanly_on_fresh_database(tmp_path, monkeypatch) -> None:
    """``alembic upgrade head`` must succeed on a brand-new empty SQLite file.

    Regression guard for the live collision: revision 0001 used to run
    ``Base.metadata.create_all`` (the FULL current schema), so tables owned by
    later migrations (scan_results, parity_mismatches, risk_outcomes, ...)
    already existed by the time those migrations ran and ``upgrade head``
    crashed with "table already exists" on a fresh database. 0001 now creates
    only its own base tables and every later migration is existence-guarded.

    Runs the chain three ways to prove idempotency: upgrade -> downgrade base
    -> upgrade, all against one fresh temp file. Runs alembic in a subprocess
    so the DATABASE_URL override cannot be polluted by the cached settings.

    This test is also the enforcement guard for the migration-maintenance rule
    documented in ``0001_initial._LATER_OWNED_TABLES``: any future migration
    that creates a table already present in models.py without registering it
    there will break ``upgrade head`` on a fresh database — and fail here.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "fresh-migration.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}
    base = [sys.executable, "-m", "alembic", "-c", "storage/alembic.ini"]

    first = subprocess.run(
        [*base, "upgrade", "head"], cwd=project_root, env=env, capture_output=True, text=True
    )
    assert first.returncode == 0, first.stderr
    assert db_path.exists()

    downgrade = subprocess.run(
        [*base, "downgrade", "base"], cwd=project_root, env=env, capture_output=True, text=True
    )
    assert downgrade.returncode == 0, downgrade.stderr

    second = subprocess.run(
        [*base, "upgrade", "head"], cwd=project_root, env=env, capture_output=True, text=True
    )
    assert second.returncode == 0, second.stderr

    # Spot-check that the tables owned by the previously-colliding migrations
    # exist after a clean upgrade.
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    for expected in ("scan_results", "parity_mismatches", "risk_outcomes", "ensemble_state"):
        assert expected in tables, f"{expected} missing after clean upgrade"
