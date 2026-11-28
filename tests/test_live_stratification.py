"""Live-token stratification in the validation harness.

The headline edge cells are dominated by dead-token structure (tokens
already dead/collapsed at score time stay dead), so the harness must
also report the edge on live tokens alone — baselined against the live
base rate, never the global one.  The dead stratum is reported
alongside so the contrast is explicit.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from validation.harness import (
    DEAD_PHASES,
    LIVE_PHASES,
    OutcomeRow,
    evaluate_band_outcomes,
)

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _row(*, asset_id: int, band: str, collapsed: bool, phase: str) -> OutcomeRow:
    return OutcomeRow(
        asset_id=asset_id,
        decision_ts=T0,
        score_id=None,
        predicted_band=band,
        risk_score=50.0,
        exit_risk=0.5,
        confidence=0.8,
        uncertainty=0.2,
        collapsed=collapsed,
        rugged=False,
        survived=not collapsed,
        phase_at_score=phase,
    )


def _fixture_rows() -> list[OutcomeRow]:
    """10 live, 10 dead, 2 unknown-phase rows with known outcomes."""
    rows: list[OutcomeRow] = []
    asset = 0
    # Live: 6 BLACK (3 collapsed, 3 survived), 4 YELLOW (1 collapsed).
    for i in range(6):
        asset += 1
        rows.append(_row(asset_id=asset, band="BLACK", collapsed=i < 3, phase="ignition"))
    for i in range(4):
        asset += 1
        rows.append(_row(asset_id=asset, band="YELLOW", collapsed=i == 0, phase="saturation"))
    # Dead: 6 BLACK (all collapsed), 4 YELLOW (all collapsed).
    for _ in range(6):
        asset += 1
        rows.append(_row(asset_id=asset, band="BLACK", collapsed=True, phase="dead"))
    for _ in range(4):
        asset += 1
        rows.append(_row(asset_id=asset, band="YELLOW", collapsed=True, phase="collapse"))
    # Unknown phase: excluded from both strata, kept in the headline cells.
    asset += 1
    rows.append(_row(asset_id=asset, band="BLACK", collapsed=True, phase="unknown"))
    asset += 1
    rows.append(_row(asset_id=asset, band="YELLOW", collapsed=False, phase="unknown"))
    return rows


def test_live_stratum_baselined_against_live_base_rate() -> None:
    rows = _fixture_rows()
    live = [r for r in rows if r.phase_at_score in LIVE_PHASES]
    cells = evaluate_band_outcomes(live, regime="synthetic", min_samples=1, stratum="live")
    by_key = {(c.output, c.metric): c for c in cells}

    black = by_key[("risk_band:live:BLACK", "collapse_precision")]
    assert black.n == 6
    assert black.value == pytest.approx(3 / 6)  # live BLACK collapse rate
    assert black.baseline_value == pytest.approx(4 / 10)  # LIVE base rate, not global

    ordinal = by_key[("risk_band:live", "ordinal_distance")]
    assert ordinal.n == 10
    assert "live-token stratum" in ordinal.note

    # A stratified call emits only prefixed outputs.
    assert not any(c.output in {"risk_band", "risk_band:BLACK"} for c in cells)


def test_dead_stratum_mirrors_live() -> None:
    rows = _fixture_rows()
    dead = [r for r in rows if r.phase_at_score in DEAD_PHASES]
    cells = evaluate_band_outcomes(dead, regime="synthetic", min_samples=1, stratum="dead")
    by_key = {(c.output, c.metric): c for c in cells}

    black = by_key[("risk_band:dead:BLACK", "collapse_precision")]
    assert black.n == 6
    assert black.value == pytest.approx(1.0)  # dead BLACK always collapses
    assert black.baseline_value == pytest.approx(1.0)  # dead base rate is 1.0

    ordinal = by_key[("risk_band:dead", "ordinal_distance")]
    assert ordinal.n == 10


def test_unstratified_call_is_unchanged() -> None:
    rows = _fixture_rows()
    cells = evaluate_band_outcomes(rows, regime="synthetic", min_samples=1)
    by_key = {(c.output, c.metric): c for c in cells}
    black = by_key[("risk_band:BLACK", "collapse_precision")]
    assert black.n == 13  # 6 live + 6 dead + 1 unknown
    assert black.value == pytest.approx(10 / 13)
    assert black.baseline_value == pytest.approx(15 / 22)  # global base rate
    assert ("risk_band:live:BLACK", "collapse_precision") not in by_key


def test_run_harness_reports_live_and_dead_strata(session) -> None:
    """End-to-end: run_harness emits both strata with their own baselines."""
    from storage import models
    from storage.repository import upsert_asset
    from tests.conftest import seed_reference
    from validation.harness import run_harness

    chain, _source = seed_reference(session)
    scored_at = T0

    def seed_outcome(*, suffix: str, band: str, collapsed: bool, phase: str) -> None:
        asset = upsert_asset(
            session,
            chain_id=chain.id,
            address=f"Strat{suffix}11111111111111111111111111111111",
            symbol=f"S{suffix[:2]}",
            name=f"Strat {suffix} Fixture",
            first_seen_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        score = models.Score(
            asset_id=asset.id,
            decision_ts=scored_at,
            observed_at=scored_at,
            hype=0.5,
            ethos=0.5,
            risk=50.0,
            liquidity_access=0.5,
            manipulation=0.1,
            confidence=0.8,
            uncertainty=0.2,
            catalyst=0.3,
            exit_risk=0.5,
            research_priority=0.5,
            risk_band=band,
            model_version="test-stratification",
        )
        session.add(score)
        session.flush()
        session.add(
            models.RiskOutcome(
                asset_id=asset.id,
                score_id=score.id,
                risk_band=band,
                scored_at=scored_at,
                lifecycle_phase_at_score=phase,
                evaluated_at=scored_at,
                collapsed=collapsed,
                rugged=False,
                survived=not collapsed,
                label_version="v3_bounded",
            )
        )

    rows = _fixture_rows()
    for i, row in enumerate(rows):
        seed_outcome(
            suffix=f"{i:02d}",
            band=row.predicted_band,
            collapsed=row.collapsed,
            phase=row.phase_at_score or "unknown",
        )
    session.commit()

    report = run_harness(session, min_samples=1)
    by_key = {(c.output, c.metric): c for c in report.cells}

    live_black = by_key[("risk_band:live:BLACK", "collapse_precision")]
    assert live_black.n == 6
    assert live_black.value == pytest.approx(3 / 6)
    assert live_black.baseline_value == pytest.approx(4 / 10)

    dead_black = by_key[("risk_band:dead:BLACK", "collapse_precision")]
    assert dead_black.value == pytest.approx(1.0)

    live_ordinal = by_key[("risk_band:live", "ordinal_distance")]
    assert live_ordinal.n == 10

    # Headline cells still cover every row, unknown phase included.
    headline_black = by_key[("risk_band:BLACK", "collapse_precision")]
    assert headline_black.n == 13
