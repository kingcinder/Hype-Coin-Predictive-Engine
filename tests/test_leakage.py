"""Automated point-in-time leakage guardrails (fabrication plan item #8).

The mission's second pillar is point-in-time evidence replayable "without
future leakage". ``docs/leakage-audit.md`` documents the per-feature query
filters; ``validation/leakage_guard.py`` enforces them at runtime. These
tests prove the enforcement bites:

- an intentionally leaky pipeline (data observed *after* the decision time
  reaching feature math) MUST raise ``LeakageViolation``;
- a clean pipeline MUST pass untouched and produce the full feature set;
- violations MUST be logged to ``SystemHealth`` (component ``leakage_guard``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from features.factory import FEATURE_NAMES, FeatureFactory
from storage import models
from tests.conftest import seed_market_asset
from validation import (
    LeakageViolation,
    check_rows_point_in_time,
    current_decision_ts,
    frozen_decision_ts,
    record_leakage_violation,
    require_decision_ts,
)
from validation.leakage_guard import LEAKAGE_GUARD_COMPONENT

DECISION = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _row(ts: datetime, observed_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(ts=ts, observed_at=observed_at, price_usd=1.0)


# ---------------------------------------------------------------------------
# check_rows_point_in_time: the exact enforcement net build_for_asset calls
# ---------------------------------------------------------------------------


def test_clean_rows_pass() -> None:
    rows = [
        _row(DECISION - timedelta(hours=2), DECISION - timedelta(hours=2)),
        _row(DECISION - timedelta(hours=1), DECISION - timedelta(minutes=30)),
        _row(DECISION, DECISION),  # observed exactly at decision time: legal
    ]
    assert check_rows_point_in_time(rows, DECISION, feature_name="market_block") == 3
    assert check_rows_point_in_time([], DECISION) == 0


def test_future_observed_row_is_caught() -> None:
    """The intentionally leaky pipeline: one row observed after decision_ts."""
    rows = [
        _row(DECISION - timedelta(hours=1), DECISION - timedelta(hours=1)),
        # A query that lost its ``observed_at`` filter would hand this row
        # to feature math - the guard must fail the build instead.
        _row(DECISION + timedelta(hours=2), DECISION + timedelta(hours=2)),
    ]
    with pytest.raises(LeakageViolation, match="newer than"):
        check_rows_point_in_time(rows, DECISION, feature_name="market_block")


def test_dict_rows_are_checked_too() -> None:
    rows = [
        {"observed_at": DECISION - timedelta(hours=1)},
        {"observed_at": DECISION + timedelta(minutes=5)},
    ]
    with pytest.raises(LeakageViolation):
        check_rows_point_in_time(rows, DECISION)


def test_rows_without_timestamps_are_skipped() -> None:
    assert check_rows_point_in_time([SimpleNamespace(price_usd=1.0)], DECISION) == 0


# ---------------------------------------------------------------------------
# frozen_decision_ts / require_decision_ts
# ---------------------------------------------------------------------------


def test_frozen_scope_roundtrip() -> None:
    assert current_decision_ts() is None
    with frozen_decision_ts(DECISION) as frozen:
        assert frozen == DECISION
        assert current_decision_ts() == DECISION
    assert current_decision_ts() is None


def test_require_decision_ts_rejects_none() -> None:
    factory = FeatureFactory()
    with pytest.raises(LeakageViolation, match="requires a decision_ts"):
        factory.build_for_asset(object(), object(), None)  # type: ignore[arg-type]


def test_require_decision_ts_rejects_mismatch_in_frozen_scope() -> None:
    @require_decision_ts
    def entry(*, decision_ts: datetime) -> str:
        return "ok"

    with frozen_decision_ts(DECISION):
        assert entry(decision_ts=DECISION) == "ok"
        with pytest.raises(LeakageViolation, match="does not match the frozen"):
            entry(decision_ts=DECISION + timedelta(hours=1))


def test_factory_method_rejects_missing_decision_ts() -> None:
    factory = FeatureFactory()
    with pytest.raises(LeakageViolation):
        factory.persist_for_assets(object(), decision_ts=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Clean end-to-end pipeline passes the guard
# ---------------------------------------------------------------------------


def test_clean_pipeline_passes_guard(session) -> None:
    """Seeded snapshots all observed at/before decision_ts: full build, no violation."""
    asset = seed_market_asset(session)
    factory = FeatureFactory()
    values = factory.build_for_asset(session, asset, DECISION)
    assert {value.name for value in values} == set(FEATURE_NAMES)
    # The market block actually consumed the seeded point-in-time data.
    by_name = {value.name: value for value in values}
    assert by_name["five_min_return"].value is not None


def test_clean_scan_persists_under_frozen_scope(session) -> None:
    """persist_for_assets freezes decision_ts for the whole scan; per-asset
    builds must carry the same value (enforced by the decorator)."""
    asset = seed_market_asset(session)
    factory = FeatureFactory()
    output = factory.persist_for_assets(session, decision_ts=DECISION)
    # seed_market_asset also creates the USDC quote asset, so both are scanned.
    assert asset.id in output
    assert {value.name for value in output[asset.id].values()} == set(FEATURE_NAMES)
    persisted = session.scalars(
        select(models.Feature).where(
            models.Feature.asset_id == asset.id,
            models.Feature.decision_ts == DECISION,
        )
    ).all()
    assert len(persisted) == len(FEATURE_NAMES)


# ---------------------------------------------------------------------------
# Violations are logged to SystemHealth
# ---------------------------------------------------------------------------


def test_violation_is_logged_to_system_health(session) -> None:
    rows = [_row(DECISION + timedelta(hours=1), DECISION + timedelta(hours=1))]
    try:
        check_rows_point_in_time(rows, DECISION, feature_name="market_block")
    except LeakageViolation as exc:
        record_leakage_violation(
            session,
            feature_name="market_block",
            decision_ts=DECISION,
            detail=str(exc),
        )
    else:  # pragma: no cover - the guard must have raised above
        raise AssertionError("leaky rows were not caught")

    row = session.scalar(
        select(models.SystemHealth).where(models.SystemHealth.component == LEAKAGE_GUARD_COMPONENT)
    )
    assert row is not None
    assert row.state == "violation"
    assert row.error_count == 1
    assert "market_block" in (row.message or "")


def test_record_leakage_violation_never_masks_the_original(session) -> None:
    # Even with a broken session the original exception must surface, not a
    # logging failure.
    record_leakage_violation(None, feature_name="market_block", detail="boom")
