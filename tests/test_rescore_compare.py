"""Integration tests for the rescore script's --compare and --min-change flags.

Uses a small in-memory fixture with 5 tokens that have known risk scores,
verifies the diff output format, sorting, min-change filtering, and that
no DB writes occur when --compare is passed (or compare is on, which
implies dry-run).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from storage import models
from storage.database import Base

# Shared with _extract_deltas so the parser can never silently diverge from
# the fixture's token set (adding/renaming a symbol keeps both in sync).
FIXTURE_SYMBOLS = ["DOGE", "PEPE", "SHIB", "BONK", "WIF"]


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
    # Using the real FEATURE_NAMES (liquidity_depth, spread_estimate, etc.) with
    # low-liquidity values yields a deterministic (elevated) risk for every
    # token — differing from each seeded risk, so --compare has rows to show.
    base_features = {
        "liquidity_depth": 5000.0,
        "pair_age_minutes": 2.0,
        "spread_estimate": 15.0,
        "buy_sell_ratio": 0.2,
        "volatility": 40.0,
        "top_holder_concentration": 0.7,
    }
    for asset in assets:
        for name, value in base_features.items():
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
) -> dict[str, int]:
    """Run rescore against the in-memory fixture session."""
    from scripts.rescore import rescore

    return rescore(
        dry_run=dry_run,
        compare=compare,
        min_change=min_change,
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
