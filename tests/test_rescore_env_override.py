"""End-to-end subprocess tests of ``scripts/rescore.py``'s real ``__main__``.

The CLI was previously untestable end-to-end: ``rescore(session=None)`` binds
the configured database, so a subprocess run always hit the real DB.
``Settings`` now resolves the ``SERPENT_DB_PATH`` / ``DATABASE_URL`` env
overrides into ``settings.database_url`` at construction (single source of
truth; see ``common.config.database_url_env_override``), letting a throwaway
SQLite file stand in — these tests drive the actual
``argparse → session_scope() → rescore`` path via ``sys.executable`` and
prove it works read-only (``--compare --dry-run``) and read-write (plain run)
against an arbitrary DB, while leaving the repo's real fleet untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from storage import models
from storage.database import Base

# Shared with tests/test_rescore_compare.py so the fixture fleet can never
# diverge from the parse assertions there. (Brings in scripts.rescore +
# storage.database at collection — lazy engines, no side effects; the guard in
# ``real_fleet_before`` still controls whether the real DB is ever touched.)
from tests.test_rescore_compare import BASE_FEATURES, FIXTURE_SYMBOLS

REPO_ROOT = Path(__file__).resolve().parents[1]
RISKS = [45.0, 62.0, 38.0, 71.0, 55.0]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_file_db(db_path: Path) -> None:
    """Create a THROWAWAY SQLite file DB with the 5-token fixture fleet."""
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with Session() as session:
        chain = models.Chain(
            slug="solana",
            name="Solana",
            vm_type="solana",
            native_symbol="SOL",
        )
        session.add(chain)
        session.flush()
        assets = []
        for i, sym in enumerate(FIXTURE_SYMBOLS, start=1):
            asset = models.Asset(
                id=i,
                chain_id=chain.id,
                symbol=sym,
                address=f"addr_{sym.lower()}",
                first_seen_at=now,
            )
            session.add(asset)
            assets.append(asset)
        session.flush()
        for i, (asset, risk) in enumerate(zip(assets, RISKS, strict=True), start=1):
            session.add(
                models.Score(
                    id=i,
                    asset_id=asset.id,
                    decision_ts=now,
                    observed_at=now,
                    model_version="test",
                    risk=risk,
                    exit_risk=risk * 0.5,
                    hype=50.0,
                    ethos=50.0,
                    liquidity_access=50.0,
                    manipulation=0.0,
                    confidence=50.0,
                    uncertainty=50.0,
                    catalyst=0.0,
                    research_priority=0.0,
                    risk_band="YELLOW",
                )
            )
        for asset in assets:
            for name, value in BASE_FEATURES.items():
                session.add(
                    models.Feature(
                        asset_id=asset.id,
                        decision_ts=now,
                        observed_at=now,
                        feature_name=name,
                        feature_value=value,
                        missing_flag=False,
                    )
                )
        session.commit()


def _risks(db_path: Path) -> tuple[int, float]:
    """(count, sum(risk)) on a throwaway file DB via an independent connection."""
    table = models.Score.__table__
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            n = conn.execute(select(func.count()).select_from(table)).scalar_one()
            total = conn.execute(select(func.sum(table.c.risk)).select_from(table)).scalar_one()
        return int(n), float(total or 0.0)
    finally:
        engine.dispose()


def _run_cli(
    db_path: Path,
    env_name: str,
    env_value: str,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the REAL __main__ path in a subprocess bound to ``db_path``.

    The binding happens purely through ``env_name``/``env_value`` (and any
    ``extra_env``); ``db_path`` documents the intended target DB for callers
    and reader comprehension.
    """
    env = {
        # Three-tier order: ambient env as the base, extra_env overrides it
        # (so an ambient variable like an inherited DATABASE_URL can't leak
        # into these tests), and the explicit ENV / env_name / PYTHONPATH
        # beat both.
        **os.environ,
        **(extra_env or {}),
        "ENV": "local-single",
        env_name: env_value,
        "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "rescore.py"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
        env=env,
    )


@pytest.fixture()
def throwaway_db(tmp_path: Path) -> Path:
    """Seed + return the path of a throwaway fleet DB (fresh per test)."""
    db_path = tmp_path / "rescore_cli.db"
    _seed_file_db(db_path)
    return db_path


@pytest.fixture(scope="module")
def real_fleet_before() -> float | None:
    """Sum of risk in the configured (real) DB before any CLI ran.

    Returns None when the real DB file isn't present (e.g. CI checkout) — in
    that case the untouched-fleet assertions are skipped.
    """
    if not (REPO_ROOT / "serpent.db").exists():
        return None
    from storage.database import SessionLocal

    with SessionLocal() as session:
        return float(session.scalar(select(func.sum(models.Score.risk))) or 0.0)


def _assert_real_fleet_untouched(real_fleet_before: float | None) -> None:
    if real_fleet_before is None:
        return  # no real DB in this checkout; nothing to guard
    from storage.database import SessionLocal

    with SessionLocal() as session:
        after = float(session.scalar(select(func.sum(models.Score.risk))) or 0.0)
    assert after == real_fleet_before, "CLI touched the real configured fleet!"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value_factory",
    [
        pytest.param(lambda p: str(p), id="bare-path"),
        pytest.param(lambda p: f"sqlite:///{p}", id="sqlite-url"),
    ],
)
def test_cli_compare_dry_run_honors_serpent_db_path(
    throwaway_db: Path,
    real_fleet_before: float | None,
    value_factory: Callable[[Path], str],
) -> None:
    """SERPENT_DB_PATH (bare path AND full sqlite:// URL forms) binds a
    --compare run to a temp DB.

    The compare output must materialize and the run must be read-only on the
    override DB (this is the exact __main__ shape CI smoke-tests in prod).
    """
    result = _run_cli(
        throwaway_db, "SERPENT_DB_PATH", value_factory(throwaway_db), "--compare", "--dry-run"
    )

    assert result.returncode == 0, result.stderr
    assert "Risk changes (5 tokens, min_change=0.0)" in result.stdout
    assert "Rescore complete (DRY RUN)" in result.stdout
    assert "Rescore complete (APPLIED)" not in result.stdout
    # Override DB untouched.
    assert _risks(throwaway_db) == (5, sum(RISKS))
    _assert_real_fleet_untouched(real_fleet_before)


def test_cli_real_write_honors_database_url(
    throwaway_db: Path, real_fleet_before: float | None
) -> None:
    """DATABASE_URL (full sqlite URL) lets the real write pass land on a temp DB.

    This is the migration shape (`python scripts/rescore.py` with no flags): it
    must APPLY to the override DB, proving the production __main__ write path is
    now integration-testable end-to-end.
    """
    url = f"sqlite:///{throwaway_db}"
    result = _run_cli(throwaway_db, "DATABASE_URL", url)

    assert result.returncode == 0, result.stderr
    assert "Rescore complete (APPLIED)" in result.stdout
    assert result.stdout.count("(DRY RUN)") == 0
    # Override DB actually rewritten: same count, different risk values.
    n, total = _risks(throwaway_db)
    assert n == 5
    assert abs(total - sum(RISKS)) > 1.0, "write pass did not change persisted risks"
    _assert_real_fleet_untouched(real_fleet_before)


def test_serpent_db_path_wins_over_database_url(
    tmp_path: Path, throwaway_db: Path, real_fleet_before: float | None
) -> None:
    """When both env vars are set, SERPENT_DB_PATH must take precedence."""
    other_db = tmp_path / "loser.db"
    _seed_file_db(other_db)
    other_before = _risks(other_db)

    url = f"sqlite:///{other_db}"
    result = _run_cli(
        throwaway_db, "SERPENT_DB_PATH", str(throwaway_db), extra_env={"DATABASE_URL": url}
    )

    assert result.returncode == 0, result.stderr
    # The SERPENT_DB_PATH DB took the real write; the DATABASE_URL DB untouched.
    n, total = _risks(throwaway_db)
    assert n == 5 and abs(total - sum(RISKS)) > 1.0
    assert _risks(other_db) == other_before
    _assert_real_fleet_untouched(real_fleet_before)


def test_cli_review_flags_via_real_main(
    tmp_path: Path, throwaway_db: Path, real_fleet_before: float | None
) -> None:
    """The new review flags survive the real ``__main__`` wiring end-to-end.

    ``--sweep --symbol-filter --limit --export-csv`` together with --compare
    must (a) be read by argparse, (b) flow into ``rescore`` via the real
    ``__main__ → session_scope`` path, (c) emit the sweep + capped diff table to
    stdout, and (d) write the filtered CSV — all while remaining read-only on
    the throwaway DB (review flags imply --compare → dry-run).
    """
    csv_path = tmp_path / "movers.csv"
    result = _run_cli(
        throwaway_db,
        "SERPENT_DB_PATH",
        str(throwaway_db),
        "--compare",
        "--sweep",
        "--symbol-filter",
        "pepe,wif",
        "--limit",
        "2",
        "--export-csv",
        str(csv_path),
    )

    assert result.returncode == 0, result.stderr
    # Sweep + review-scoped header on stdout.
    assert "Mover sweep" in result.stdout
    assert "p90=" in result.stdout
    assert "Risk changes (2 tokens" in result.stdout
    assert "Rescore complete (DRY RUN)" in result.stdout
    assert "Rescore complete (APPLIED)" not in result.stdout
    # CSV written with the filtered rows (PEPE + WIF only).
    import csv as csv_mod

    assert csv_path.exists()
    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv_mod.reader(fh))
    assert rows[0] == ["symbol", "asset_id", "decision_ts", "old_risk", "new_risk", "delta"]
    assert {row[0] for row in rows[1:]} == {"PEPE", "WIF"}
    # --limit 2 caps the CSV too: header + 2 rows.
    assert len(rows) == 3
    # Override DB untouched — review flags never write.
    assert _risks(throwaway_db) == (5, sum(RISKS))
    _assert_real_fleet_untouched(real_fleet_before)


def test_cli_review_flag_implies_dry_run(
    throwaway_db: Path, real_fleet_before: float | None
) -> None:
    """A review flag with NO explicit --compare/--dry-run still stays read-only:
    the CLI forces compare (→ dry-run) when --sweep / --export-csv / --top-pct
    are present, so the migration-safety contract holds for every review shape.
    """
    result = _run_cli(throwaway_db, "SERPENT_DB_PATH", str(throwaway_db), "--sweep")

    assert result.returncode == 0, result.stderr
    assert "Mover sweep" in result.stdout
    assert "Rescore complete (DRY RUN)" in result.stdout
    assert "Rescore complete (APPLIED)" not in result.stdout
    assert _risks(throwaway_db) == (5, sum(RISKS))
    _assert_real_fleet_untouched(real_fleet_before)
