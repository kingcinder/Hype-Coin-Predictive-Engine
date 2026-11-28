"""Ticket 1 step 5 (M3): a collapse=0 label must be earned by evidence.

Before this change every label generator wrote collapse=0 whenever at
least one future snapshot existed at any price — so the most thinly
observed (and most death-prone) tokens trained as non-collapsers, the
same poisoning dbfe727 fixed for outcome labels. A zero now requires
forward-evidence density: enough snapshots across the window, including
at least one in its final quartile. Otherwise the point is censored —
no collapse label is written. A collapse=1 needs only the observation
itself, so positives are never gated.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from forecast.labels import (
    LABEL_COLLAPSE,
    LABEL_FORWARD_MAX_GAIN,
    LABEL_IGNITION,
    LabelEngine,
    forward_evidence_sufficient,
    seed_labels_at_feature_timestamps,
)
from storage import models
from storage.repository import (
    insert_market_snapshot_once,
    upsert_asset,
    upsert_feature,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0)  # naive for SQLite compatibility
WINDOW = timedelta(hours=24)
START = datetime(2026, 5, 1, 0, 0, tzinfo=None)


def _ts(hours: float) -> datetime:
    return START + timedelta(hours=hours)


# ── the density predicate ────────────────────────────────────────────────────


def test_evidence_empty_is_insufficient() -> None:
    assert not forward_evidence_sufficient([], start=START, window=WINDOW)


def test_evidence_dense_with_final_quartile_is_sufficient() -> None:
    stamps = [_ts(h) for h in (1, 6, 12, 20, 23)]
    assert forward_evidence_sufficient(stamps, start=START, window=WINDOW)


def test_evidence_without_final_quartile_is_insufficient() -> None:
    # Plenty of snapshots, but coverage stops halfway through the window:
    # a collapse in the unobserved second half would be invisible.
    stamps = [_ts(h) for h in (1, 3, 5, 7, 9, 11)]
    assert not forward_evidence_sufficient(stamps, start=START, window=WINDOW)


def test_evidence_too_few_snapshots_is_insufficient() -> None:
    # One snapshot in the final quartile but only two overall for a 24h
    # window (minimum scales with window length: one per 6h -> 4).
    stamps = [_ts(2), _ts(23)]
    assert not forward_evidence_sufficient(stamps, start=START, window=WINDOW)


def test_evidence_minimum_scales_with_window() -> None:
    short = timedelta(hours=6)
    stamps = [_ts(1), _ts(5.5)]
    assert forward_evidence_sufficient(stamps, start=START, window=short)


# ── generator integration ────────────────────────────────────────────────────


def _seed_sparse_asset(session, *, symbol: str, snapshot_hours: list[int], price: float = 1.0):
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"Addr{symbol}11111111111111111111111111111111111",
        symbol=symbol,
        name=symbol,
        first_seen_at=T0,
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"Quote{symbol}1111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=T0 - timedelta(days=365),
    )
    _pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=f"Pair{symbol}1111111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0,
    )
    for hour in snapshot_hours:
        ts = T0 + timedelta(hours=hour)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=1_000,
            buys=10,
            sells=5,
        )
    return asset


def _labels_by_type(session, asset_id: int) -> dict[str, list[str]]:
    rows = session.scalars(select(models.Label).where(models.Label.asset_id == asset_id)).all()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row.label_type, []).append(row.label_value)
    return out


def test_label_engine_censors_thin_collapse_zero(session) -> None:
    # Snapshots only at hours 0-2: the forward window of hour 0 is covered
    # for two hours out of twenty-four. Flat prices would have produced a
    # collapse=0 before the fix.
    asset = _seed_sparse_asset(session, symbol="THIN", snapshot_hours=[0, 1, 2])
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    by_type = _labels_by_type(session, asset.id)
    assert by_type.get(LABEL_IGNITION)  # ignition zeros are not gated
    assert LABEL_COLLAPSE not in by_type  # collapse zero censored, not written


def test_label_engine_dense_coverage_still_writes_collapse_zero(session) -> None:
    asset = _seed_sparse_asset(session, symbol="DENSE", snapshot_hours=list(range(30)))
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    by_type = _labels_by_type(session, asset.id)
    assert "0" in by_type.get(LABEL_COLLAPSE, [])


def test_bootstrap_censors_thin_collapse_zero(session) -> None:
    asset = _seed_sparse_asset(session, symbol="BTHIN", snapshot_hours=[0, 11, 12, 13])
    upsert_feature(
        session,
        asset_id=asset.id,
        decision_ts=T0 + timedelta(hours=11),
        feature_name="liquidity_depth",
        feature_value=200_000.0,
        source_count=1,
        freshness_score=1.0,
        missing_flag=False,
    )
    session.commit()
    counts = seed_labels_at_feature_timestamps(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert counts["collapse"] == 0
    by_type = _labels_by_type(session, asset.id)
    assert by_type.get(LABEL_IGNITION) == ["0"]
    assert LABEL_COLLAPSE not in by_type


def test_dense_generator_censors_thin_collapse_zero(session) -> None:
    from data_lake.labels import generate_dense_labels

    asset = _seed_sparse_asset(session, symbol="GTHIN", snapshot_hours=[0, 1])
    session.commit()
    generate_dense_labels(session, decision_ts=T0 + timedelta(hours=48), forward_hours=24)
    session.commit()
    by_type = _labels_by_type(session, asset.id)
    assert by_type.get(LABEL_IGNITION)
    assert LABEL_COLLAPSE not in by_type


def test_collapse_positive_never_gated(session) -> None:
    # A single observed crash writes collapse=1 even with thin coverage:
    # the positive is the observation itself.
    chain_asset = _seed_sparse_asset(session, symbol="CRASH1", snapshot_hours=[0, 1], price=1.0)
    # second snapshot crashes: reseed manually at a crashed price
    rows = session.scalars(select(models.MarketSnapshot)).all()
    for row in rows:
        if row.ts.replace(tzinfo=None) == T0 + timedelta(hours=1):
            row.price_usd = 0.2
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    by_type = _labels_by_type(session, chain_asset.id)
    assert "1" in by_type.get(LABEL_COLLAPSE, [])


# ── gain-side mirrors (Task 2: symmetric censoring) ─────────────────────────
#
# A forward_max_gain_pct below the ignition threshold is the same kind of
# claim as collapse=0 — "nothing happened in this window" — so it is held
# to the same evidence-density rule. Each case below mirrors a collapse
# case above.


def test_label_engine_censors_thin_gain_zero(session) -> None:
    # Mirror of test_label_engine_censors_thin_collapse_zero: coverage for
    # two hours out of twenty-four, flat prices — the "no pump" gain label
    # is censored, not written.
    asset = _seed_sparse_asset(session, symbol="GTHIN2", snapshot_hours=[0, 1, 2])
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    by_type = _labels_by_type(session, asset.id)
    assert by_type.get(LABEL_IGNITION)  # ignition zeros are not gated
    assert LABEL_FORWARD_MAX_GAIN not in by_type


def test_label_engine_gain_zero_censored_without_final_quartile(session) -> None:
    # Mirror of test_evidence_without_final_quartile_is_insufficient at the
    # generator level: twelve snapshots clear the minimum count, but
    # coverage stops at hour 11 — a pump in the unobserved second half
    # would be invisible, so the flat window earns no gain label.
    asset = _seed_sparse_asset(session, symbol="GNOFQ", snapshot_hours=list(range(12)))
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    by_type = _labels_by_type(session, asset.id)
    assert LABEL_FORWARD_MAX_GAIN not in by_type


def test_label_engine_dense_coverage_still_writes_gain_zero(session) -> None:
    # Mirror of test_label_engine_dense_coverage_still_writes_collapse_zero.
    asset = _seed_sparse_asset(session, symbol="GDENSE", snapshot_hours=list(range(30)))
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    by_type = _labels_by_type(session, asset.id)
    assert "0.000000" in by_type.get(LABEL_FORWARD_MAX_GAIN, [])


def test_bootstrap_censors_thin_gain_zero(session) -> None:
    # Mirror of test_bootstrap_censors_thin_collapse_zero.
    asset = _seed_sparse_asset(session, symbol="BGTHIN", snapshot_hours=[0, 11, 12, 13])
    upsert_feature(
        session,
        asset_id=asset.id,
        decision_ts=T0 + timedelta(hours=11),
        feature_name="liquidity_depth",
        feature_value=200_000.0,
        source_count=1,
        freshness_score=1.0,
        missing_flag=False,
    )
    session.commit()
    counts = seed_labels_at_feature_timestamps(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert counts["forward_max_gain"] == 0
    by_type = _labels_by_type(session, asset.id)
    assert by_type.get(LABEL_IGNITION) == ["0"]
    assert LABEL_FORWARD_MAX_GAIN not in by_type


def test_dense_generator_censors_thin_gain_zero(session) -> None:
    # Mirror of test_dense_generator_censors_thin_collapse_zero.
    from data_lake.labels import generate_dense_labels

    asset = _seed_sparse_asset(session, symbol="DGTHIN", snapshot_hours=[0, 1])
    session.commit()
    generate_dense_labels(session, decision_ts=T0 + timedelta(hours=48), forward_hours=24)
    session.commit()
    by_type = _labels_by_type(session, asset.id)
    assert by_type.get(LABEL_IGNITION)
    assert LABEL_FORWARD_MAX_GAIN not in by_type


# ── replay-path mirrors (four-path symmetry) ────────────────────────────────
#
# The data-lake replay path was the last generator writing collapse=0
# unconditionally. Its zeros are held to the same evidence-density rule
# as the other three paths; positives are never gated.


def _seed_replay_feature(session, asset_id: int) -> None:
    feature = upsert_feature(
        session,
        asset_id=asset_id,
        decision_ts=T0,
        feature_name="liquidity_depth",
        feature_value=200_000.0,
        source_count=1,
        freshness_score=1.0,
        missing_flag=False,
    )
    # The replay only trusts features observed by their own decision time.
    feature.observed_at = T0


def test_replay_censors_thin_collapse_zero(session) -> None:
    # Mirror of test_label_engine_censors_thin_collapse_zero: snapshots
    # only at hours 0-2, so the feature point at hour 0 has two forward
    # observations out of twenty-four. Flat prices would have produced a
    # collapse=0 before the fix.
    from data_lake.labels import replay_historical_labels

    asset = _seed_sparse_asset(session, symbol="RTHIN", snapshot_hours=[0, 1, 2])
    _seed_replay_feature(session, asset.id)
    session.commit()
    counts = replay_historical_labels(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert counts["collapse_created"] == 0
    by_type = _labels_by_type(session, asset.id)
    assert by_type.get(LABEL_IGNITION) == ["0"]  # ignition zeros are not gated
    assert LABEL_COLLAPSE not in by_type  # collapse zero censored, not written


def test_replay_dense_coverage_still_writes_collapse_zero(session) -> None:
    # Mirror of test_label_engine_dense_coverage_still_writes_collapse_zero.
    from data_lake.labels import replay_historical_labels

    asset = _seed_sparse_asset(session, symbol="RDENSE", snapshot_hours=list(range(30)))
    _seed_replay_feature(session, asset.id)
    session.commit()
    counts = replay_historical_labels(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert counts["collapse_created"] == 1
    by_type = _labels_by_type(session, asset.id)
    assert "0" in by_type.get(LABEL_COLLAPSE, [])


def test_replay_collapse_positive_never_gated(session) -> None:
    # Mirror of test_collapse_positive_never_gated: a single observed
    # crash writes collapse=1 even with thin coverage.
    from data_lake.labels import replay_historical_labels

    asset = _seed_sparse_asset(session, symbol="RCRASH", snapshot_hours=[0, 1], price=1.0)
    rows = session.scalars(select(models.MarketSnapshot)).all()
    for row in rows:
        if row.ts.replace(tzinfo=None) == T0 + timedelta(hours=1):
            row.price_usd = 0.2
    _seed_replay_feature(session, asset.id)
    session.commit()
    counts = replay_historical_labels(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert counts["collapse_created"] == 1
    by_type = _labels_by_type(session, asset.id)
    assert "1" in by_type.get(LABEL_COLLAPSE, [])


def test_gain_positive_never_gated(session) -> None:
    # Mirror of test_collapse_positive_never_gated: a single observed pump
    # writes the gain label even with thin coverage — the positive is the
    # observation itself.
    asset = _seed_sparse_asset(session, symbol="PUMP1", snapshot_hours=[0, 1], price=1.0)
    rows = session.scalars(select(models.MarketSnapshot)).all()
    for row in rows:
        if row.ts.replace(tzinfo=None) == T0 + timedelta(hours=1):
            row.price_usd = 3.5
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    by_type = _labels_by_type(session, asset.id)
    assert "2.500000" in by_type.get(LABEL_FORWARD_MAX_GAIN, [])
