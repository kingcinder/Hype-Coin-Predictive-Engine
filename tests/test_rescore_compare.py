"""Integration tests for the rescore script's --compare and --min-change flags.

Uses a small in-memory fixture with 5 tokens that have known risk scores,
verifies the diff output format, sorting, min-change filtering, and that
no DB writes occur when --compare is passed (or compare is on, which
implies dry-run).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scoring.formulas import compute_scores
from scripts.rescore import rescore
from storage import models
from storage.database import Base
from tests.conftest import TrackingScope

# Shared with _extract_deltas so the parser can never silently diverge from
# the fixture's token set (adding/renaming a symbol keeps both in sync).
FIXTURE_SYMBOLS = ["DOGE", "PEPE", "SHIB", "BONK", "WIF"]

# Real FEATURE_NAMES with low-liquidity values: compute_scores yields a
# deterministic (elevated) risk for every token — differing from each seeded
# risk, so --compare has rows to show and the commit-path test can detect
# a persisted write. Shared by the fixture and all tests deliberately.
BASE_FEATURES = {
    "liquidity_depth": 5000.0,
    "pair_age_minutes": 2.0,
    "spread_estimate": 15.0,
    "buy_sell_ratio": 0.2,
    "volatility": 40.0,
    "top_holder_concentration": 0.7,
}

# The 11 fields the rescore write block persists per score
# (scripts/rescore.py _rescore_in_session), in assignment order. The
# commit-path test snapshots the seeded baseline and the live
# compute_scores oracle over exactly this list, so adding/renaming a field
# in the write block keeps the test in sync automatically (mirroring the
# FIXTURE_SYMBOLS pattern).
SCORE_WRITE_FIELDS = [
    "risk",
    "exit_risk",
    "hype",
    "ethos",
    "liquidity_access",
    "manipulation",
    "confidence",
    "uncertainty",
    "catalyst",
    "research_priority",
    "risk_band",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fixture_session() -> Session:
    """Create an in-memory SQLite DB with schema + 5 fixture tokens."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    chain = models.Chain(
        slug="solana",
        name="Solana",
        vm_type="solana",
        native_symbol="SOL",
    )
    session.add(chain)
    session.flush()

    # Insert assets
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assets = []
    for i, sym in enumerate(FIXTURE_SYMBOLS, start=1):
        a = models.Asset(
            id=i,
            chain_id=chain.id,
            symbol=sym,
            address=f"addr_{sym.lower()}",
            first_seen_at=now,
        )
        session.add(a)
        assets.append(a)
    session.flush()

    # Insert scores with intentionally varied risk values
    risk_values = [45.0, 62.0, 38.0, 71.0, 55.0]
    for i, (asset, risk) in enumerate(zip(assets, risk_values, strict=True), start=1):
        s = models.Score(
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
        session.add(s)
    session.flush()

    # Insert features that will produce known new risk values via compute_scores.
    for asset in assets:
        for name, value in BASE_FEATURES.items():
            f = models.Feature(
                asset_id=asset.id,
                decision_ts=now,
                observed_at=now,
                feature_name=name,
                feature_value=value,
                missing_flag=False,
            )
            session.add(f)
    session.flush()

    yield session
    session.close()


def _run_rescore(
    session: Session,
    *,
    dry_run: bool = True,
    compare: bool = True,
    min_change: float = 0.0,
    limit: int | None = None,
    symbol_filters: list[str] | None = None,
    top_pct: float | None = None,
    sweep: bool = False,
    export_csv: str | None = None,
) -> dict[str, int]:
    """Run rescore against the in-memory fixture session."""
    from scripts.rescore import rescore

    return rescore(
        dry_run=dry_run,
        compare=compare,
        min_change=min_change,
        limit=limit,
        symbol_filters=symbol_filters,
        top_pct=top_pct,
        sweep=sweep,
        export_csv=export_csv,
        session=session,
    )


def _extract_deltas(output: str) -> list[float]:
    """Pull the signed delta column out of the --compare table rows."""
    symbols = set(FIXTURE_SYMBOLS)
    deltas: list[float] = []
    for line in output.splitlines():
        parts = line.split()
        # Table rows are exactly "<symbol> <old> <new> <delta>".
        if len(parts) != 4 or parts[0] not in symbols:
            continue
        try:
            deltas.append(float(parts[3].lstrip("+")))
        except ValueError:
            continue
    return deltas


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRescoreCompare:
    """Tests for --compare and --min-change flags."""

    def test_compare_does_not_write(self, fixture_session: Session) -> None:
        """--compare must not modify the scores table."""
        old_scores = fixture_session.scalars(select(models.Score)).all()
        old_risks = {s.asset_id: s.risk for s in old_scores}

        result = _run_rescore(fixture_session, dry_run=True, compare=True, min_change=0.0)

        fixture_session.expire_all()
        for s in fixture_session.scalars(select(models.Score)).all():
            assert s.risk == old_risks[s.asset_id], f"Score {s.id} was modified!"

        assert result["compare_rows"] > 0

    def test_compare_output_format(
        self, fixture_session: Session, capsys: pytest.CaptureFixture
    ) -> None:
        """--compare prints a table with Symbol, Old, New, Delta columns."""
        _run_rescore(fixture_session, dry_run=True, compare=True, min_change=0.0)
        output = capsys.readouterr().out

        assert "Symbol" in output
        assert "Old" in output
        assert "New" in output
        assert "Delta" in output
        for sym in FIXTURE_SYMBOLS:
            assert sym in output, f"{sym} missing from --compare output"

    def test_compare_sorted_by_abs_delta(
        self, fixture_session: Session, capsys: pytest.CaptureFixture
    ) -> None:
        """--compare output is sorted by |delta| descending."""
        _run_rescore(fixture_session, dry_run=True, compare=True, min_change=0.0)
        output = capsys.readouterr().out

        deltas = _extract_deltas(output)
        assert len(deltas) == 5, f"expected 5 delta rows, got {deltas}"
        for i in range(len(deltas) - 1):
            assert abs(deltas[i]) >= abs(deltas[i + 1]), (
                f"Not sorted: |{deltas[i]}| < |{deltas[i + 1]}| at positions {i}, {i + 1}"
            )

    def test_min_change_filters_output(
        self, fixture_session: Session, capsys: pytest.CaptureFixture
    ) -> None:
        """--min-change excludes tokens with small risk deltas."""
        result = _run_rescore(fixture_session, dry_run=True, compare=True, min_change=100.0)
        output = capsys.readouterr().out

        assert result["compare_rows"] == 0
        # The header should still appear (for transparency).
        assert "Risk changes" in output
        # No token rows are printed when nothing clears the threshold.
        assert _extract_deltas(output) == []

    def test_compare_implies_dry_run(self, fixture_session: Session) -> None:
        """--compare must force dry_run even if dry_run=False is passed."""
        old_scores = fixture_session.scalars(select(models.Score)).all()
        old_risks = {s.asset_id: s.risk for s in old_scores}

        _run_rescore(fixture_session, dry_run=False, compare=True, min_change=0.0)

        fixture_session.expire_all()
        for s in fixture_session.scalars(select(models.Score)).all():
            assert s.risk == old_risks[s.asset_id], "compare=True should force dry_run!"

    def test_session_none_uses_own_session_cycle(
        self,
        fixture_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The CLI route (``session=None``) must use its own session cycle.

        Runs with ``dry_run=False + compare=True`` — the exact argument shape
        ``python scripts/rescore.py --compare`` produces — so this also proves
        ``--compare`` forces dry-run for real callers through the real path:
        as a headless CLI (no caller session to notice), a stray commit would
        be invisible; here the summary line and the closed scope prove the
        session was owned by ``rescore`` and never written.
        """
        scope = TrackingScope(fixture_session)
        monkeypatch.setattr("scripts.rescore.session_scope", lambda: scope)

        result = rescore(dry_run=False, compare=True, min_change=0.0)
        out = capsys.readouterr().out

        # Own cycle: entered exactly once, and the session was closed on exit.
        assert scope.entered == 1
        assert scope.closed is True
        # Worked end-to-end over the fixture fleet.
        assert result["updated"] == 5
        assert result["compare_rows"] == 5
        # compare=True forced dry_run through the real-caller path: the summary
        # says DRY RUN, never APPLIED — no commit happened.
        assert "Rescore complete (DRY RUN)" in out
        assert "Rescore complete (APPLIED)" not in out

    def test_explicit_session_bypasses_default_scope(
        self, fixture_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``session=`` kwarg is the *only* injection route.

        With an explicit session, the default ``session_scope`` must never be
        entered — pinning the seam's boundary: the CLI path can't smuggle a
        session in (it passes none), and a programmatic call that passes one
        never touches the production session cycle.
        """

        def _explode() -> NoReturn:
            raise AssertionError("session_scope must not be entered when session= is passed")

        monkeypatch.setattr("scripts.rescore.session_scope", _explode)
        result = rescore(dry_run=True, compare=True, min_change=0.0, session=fixture_session)

        assert result["updated"] == 5  # ran over the injected session, no scope involved
        assert result["compare_rows"] == 5


class TestRescoreReviewFilters:
    """The review-only flags: --limit, --symbol-filter, --top-pct, --sweep,
    --export-csv.

    Contract (pinned here so a future edit can't silently blur it): these flags
    shape the diff *review* only — the write pass always rescores every
    evaluated token, never a subset of them. ``result["compare_rows"]`` reports
    the count AFTER symbol/top-pct filtering (the set the operator reviews),
    while ``--limit`` only caps how many rows are printed.
    """

    def test_symbol_filter_restricts_review_rows(
        self, fixture_session: Session, capsys: pytest.CaptureFixture
    ) -> None:
        """--symbol-filter (case-insensitive substring) narrows the diff set."""
        result = _run_rescore(
            fixture_session, dry_run=True, compare=True, min_change=0.0, symbol_filters=["pepe"]
        )
        out = capsys.readouterr().out

        assert result["compare_rows"] == 1
        assert "PEPE" in out
        assert (
            "DOGE" not in out.split("--- Risk changes")[1]
        )  # header row region only filtered rows

    def test_symbol_filter_comma_flatten(self, fixture_session: Session) -> None:
        """Multiple filters (comma-separated in one flag) all apply."""
        result = _run_rescore(
            fixture_session,
            dry_run=True,
            compare=True,
            min_change=0.0,
            symbol_filters=["DOGE, WIF"],
        )
        assert result["compare_rows"] == 2

    def test_symbol_filter_never_shrinks_write_pass(self, fixture_session: Session) -> None:
        """Filters are review-only: a real (dry_run=False) pass still rescores
        the full fleet even with a restrictive --symbol-filter."""
        before = {s.id: s.risk for s in fixture_session.scalars(select(models.Score)).all()}

        result = _run_rescore(
            fixture_session,
            dry_run=False,
            compare=False,
            min_change=0.0,
            symbol_filters=["PEPE"],
        )
        assert result["updated"] == 5  # all 5 tokens rescored, not just PEPE

        # Every token's persisted risk changed on disk — the write pass is
        # provably unfiltered despite the review-only symbol filter.
        fixture_session.expire_all()
        after = {s.id: s.risk for s in fixture_session.scalars(select(models.Score)).all()}
        assert after != before
        assert all(after[sid] != risk for sid, risk in before.items()), (
            "write pass was filtered: a token that should have moved is unchanged"
        )

    def test_limit_caps_printed_rows(
        self, fixture_session: Session, capsys: pytest.CaptureFixture
    ) -> None:
        """--limit caps console rows to the top N by |delta|, and the
        "... more" note reflects the capped-off remainder."""
        _run_rescore(fixture_session, dry_run=True, compare=True, min_change=0.0, limit=2)
        out = capsys.readouterr().out

        assert out.count("more (showing top 2 by |delta|)") == 1
        # Only two delta rows survive the cap.
        assert len(_extract_deltas(out)) == 2

    def test_top_pct_keeps_top_decile(
        self, fixture_session: Session, capsys: pytest.CaptureFixture
    ) -> None:
        """--top-pct 10 keeps only the top 10% of movers by |delta|."""
        _run_rescore(fixture_session, dry_run=True, compare=True, min_change=0.0, top_pct=10)
        out = capsys.readouterr().out

        # 5 tokens × top 10% = ceil(0.5) = 1 row.
        deltas = _extract_deltas(out)
        assert len(deltas) == 1

    def test_min_change_sweep_prints_cutoffs(
        self, fixture_session: Session, capsys: pytest.CaptureFixture
    ) -> None:
        """--sweep prints percentiles + threshold counts so an operator can
        pick a --min-change cutoff before the write pass."""
        _run_rescore(fixture_session, dry_run=True, compare=True, min_change=0.0, sweep=True)
        out = capsys.readouterr().out

        assert "Mover sweep" in out
        assert "p50=" in out
        assert "p90=" in out
        assert "top 10% of movers sit at |delta| >=" in out
        # The 10.0 threshold row is right-padded to width 5, mirroring the
        # format string in _print_mover_sweep — assert the exact printed token.
        assert f"|delta| >= {10.0:>5.1f}:" in out

    def test_export_csv_writes_filtered_rows(
        self, fixture_session: Session, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        """--export-csv materializes the filtered diff table with header + all
        rows (symbol-filter applies; --limit does not truncate the file)."""
        csv_path = tmp_path / "movers.csv"
        _run_rescore(
            fixture_session,
            dry_run=True,
            compare=True,
            min_change=0.0,
            symbol_filters=["pepe, wif"],
            export_csv=str(csv_path),
        )
        out = capsys.readouterr().out
        assert "Exported 2 mover rows" in out

        import csv as csv_mod

        with csv_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv_mod.reader(fh))
        assert rows[0] == ["symbol", "asset_id", "decision_ts", "old_risk", "new_risk", "delta"]
        data = rows[1:]
        assert len(data) == 2  # PEPE + WIF, filtered; not truncated by the 50-row cap
        symbols = {row[0] for row in data}
        assert symbols == {"PEPE", "WIF"}
        # delta column is new - old arithmetically.
        for row in data:
            old, new, delta = float(row[3]), float(row[4]), float(row[5])
            assert delta == pytest.approx(new - old)

    def test_export_csv_respects_limit(
        self, fixture_session: Session, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        """An explicit --limit caps the exported CSV to the top N rows too."""
        csv_path = tmp_path / "movers_limit.csv"
        _run_rescore(
            fixture_session,
            dry_run=True,
            compare=True,
            min_change=0.0,
            limit=2,
            export_csv=str(csv_path),
        )
        out = capsys.readouterr().out
        assert "Exported 2 mover rows" in out

        import csv as csv_mod

        with csv_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv_mod.reader(fh))
        assert len(rows) == 3  # header + 2 capped rows

    def test_session_none_commit_path_persists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The real migration shape — ``session=None`` with no ``--compare`` —
        commits through its own session cycle, visibly.

        The dry-run fork is pinned above; this is the write path a production
        rescore actually takes. The scope yields a session over a file-backed
        DB, so a second connection can prove the commit landed after the
        session was handed back to its owner and closed.

        The write block persists the whole ``ScoreResult`` — not just risk —
        so every field in ``SCORE_WRITE_FIELDS`` is pinned on a fresh
        connection against both the seeded baseline (it must have changed)
        and a live ``compute_scores`` oracle (it must equal the recomputed
        value the script was supposed to write). ``catalyst`` and
        ``research_priority`` recompute to 0.0 for this feature set, so they
        are seeded at sentinel 100.0 — a 0.0 seed would make the
        changed-value check blind to a missing write.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'rescore_commit.db'}")
        Base.metadata.create_all(engine)
        Seeder = sessionmaker(bind=engine, expire_on_commit=False)

        now = datetime(2026, 1, 1, tzinfo=UTC)
        with Seeder() as seed:
            chain = models.Chain(
                slug="solana",
                name="Solana",
                vm_type="solana",
                native_symbol="SOL",
            )
            seed.add(chain)
            seed.flush()
            seed.add(
                models.Asset(
                    id=1,
                    chain_id=chain.id,
                    symbol="DOGE",
                    address="addr_doge",
                    first_seen_at=now,
                )
            )
            seed.add(
                models.Score(
                    id=1,
                    asset_id=1,
                    decision_ts=now,
                    observed_at=now,
                    model_version="test",
                    # Every field is seeded to a value provably different from
                    # what compute_scores returns for BASE_FEATURES, so an
                    # unchanged field after the commit is always detectable.
                    risk=45.0,
                    exit_risk=22.5,
                    hype=50.0,
                    ethos=50.0,
                    liquidity_access=50.0,
                    manipulation=0.0,
                    confidence=50.0,
                    uncertainty=50.0,
                    catalyst=100.0,
                    research_priority=100.0,
                    risk_band="YELLOW",
                )
            )
            for name, value in BASE_FEATURES.items():
                seed.add(
                    models.Feature(
                        asset_id=1,
                        decision_ts=now,
                        observed_at=now,
                        feature_name=name,
                        feature_value=value,
                        missing_flag=False,
                    )
                )
            seed.commit()

        scope = TrackingScope(Seeder())
        monkeypatch.setattr("scripts.rescore.session_scope", lambda: scope)

        # Baseline + oracle, pre-migration: the seeded field values, and the
        # value the live formula would compute for them on this same DB.
        # (assess_risk consults the DB only for adaptive calibration rows,
        # which this fresh file DB doesn't have, so the oracle is exact.)
        with Seeder() as pre:
            seeded_row = pre.scalars(select(models.Score)).one()
            seeded_values = {name: getattr(seeded_row, name) for name in SCORE_WRITE_FIELDS}
            expected = compute_scores(BASE_FEATURES, [], session=pre)

        # compare=False keeps dry_run False: the own-cycle path must commit.
        result = rescore(dry_run=False, compare=False, min_change=0.0)

        assert scope.entered == 1
        assert scope.closed is True
        assert result["updated"] == 1
        assert result["compare_rows"] == 0  # compare off → no diff table

        # Fresh connection to the same file: the write is durable, proving the
        # commit happened inside the own session cycle before close. Every
        # persisted field must both differ from its seeded value (the write
        # landed) and equal the live-computed oracle (it was written
        # correctly, not merely touched).
        with Seeder() as check:
            saved = check.scalars(select(models.Score)).one()
            for name in SCORE_WRITE_FIELDS:
                persisted = getattr(saved, name)
                assert persisted != seeded_values[name], (
                    f"{name} unchanged after rescore commit (still {seeded_values[name]!r})"
                )
                if name == "risk_band":
                    assert persisted == expected.risk_band.value
                else:
                    assert persisted == pytest.approx(getattr(expected, name))
