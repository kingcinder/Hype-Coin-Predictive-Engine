"""Task 2: continuous forward labels, censored symmetrically with collapse=0.

`LabelEngine.generate` and `seed_labels_at_feature_timestamps` used to
compute the forward peak/trough and then discard everything except the
binary threshold crossings. The forward path is now persisted as
continuous label types (max gain, max drawdown, hours to peak, hours to
trough) from every generation path. A `forward_max_gain_pct` below the
ignition threshold is a "no pump" claim about the whole window, so —
exactly like collapse=0 — it is written only when forward-evidence
density earns it; at/above the threshold the observed pump is itself the
evidence and the label is never gated. The sibling measurements
(drawdown, hours to peak/trough) are written whenever the gain label is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from forecast.labels import (
    LABEL_FORWARD_MAX_DRAWDOWN,
    LABEL_FORWARD_MAX_GAIN,
    LABEL_HOURS_TO_PEAK,
    LABEL_HOURS_TO_TROUGH,
    LABEL_IGNITION,
    LabelEngine,
    forward_path,
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


def _seed_asset(session, *, symbol: str, points: list[tuple[int, float]]):
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
    for hour, price in points:
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
    return asset, pair, source


def _label_value(session, asset_id: int, ts: datetime, label_type: str) -> str | None:
    row = session.scalar(
        select(models.Label).where(
            models.Label.asset_id == asset_id,
            models.Label.ts == ts,
            models.Label.label_type == label_type,
        )
    )
    return row.label_value if row is not None else None


# ── the forward_path helper ──────────────────────────────────────────────────


def test_forward_path_captures_peak_trough_and_timing():
    start = datetime(2026, 11, 20, tzinfo=UTC)
    rows = [
        (start + timedelta(hours=2), 1.50),
        (start + timedelta(hours=5), 3.00),  # peak: +200% at 5h
        (start + timedelta(hours=9), 0.40),  # trough: -60% at 9h
        (start + timedelta(hours=12), 1.10),
    ]
    path = forward_path(1.00, start, rows)
    assert path is not None
    assert path.max_gain_pct == pytest.approx(2.0)
    assert path.max_drawdown_pct == pytest.approx(-0.6)
    assert path.hours_to_peak == pytest.approx(5.0)
    assert path.hours_to_trough == pytest.approx(9.0)


def test_forward_path_empty_rows_returns_none():
    start = datetime(2026, 11, 20, tzinfo=UTC)
    assert forward_path(1.00, start, []) is None


def test_forward_path_tie_breaks_to_earliest_timestamp():
    start = datetime(2026, 11, 20, tzinfo=UTC)
    rows = [
        (start + timedelta(hours=8), 2.00),
        (start + timedelta(hours=3), 2.00),  # same peak price, earlier
        (start + timedelta(hours=6), 0.50),
        (start + timedelta(hours=4), 0.50),  # same trough price, earlier
    ]
    path = forward_path(1.00, start, rows)
    assert path is not None
    assert path.hours_to_peak == pytest.approx(3.0)
    assert path.hours_to_trough == pytest.approx(4.0)


# ── LabelEngine generation ───────────────────────────────────────────────────


def test_small_gain_label_censored_when_forward_evidence_thin(session):
    # Two snapshots in a 24h window, max gain +3% (< ignition threshold):
    # the "no pump" claim is NOT earned -> no forward_max_gain_pct row,
    # mirroring the collapse=0 density rule.
    asset, _pair, _source = _seed_asset(
        session, symbol="SMALL", points=[(0, 1.0), (1, 1.03), (2, 1.0)]
    )
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_GAIN) is None
    # The siblings are measurements of the same claim: not written either.
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_DRAWDOWN) is None
    assert _label_value(session, asset.id, T0, LABEL_HOURS_TO_PEAK) is None
    assert _label_value(session, asset.id, T0, LABEL_HOURS_TO_TROUGH) is None
    # The binary ignition zero is not density-gated and still lands.
    assert _label_value(session, asset.id, T0, LABEL_IGNITION) == "0"


def test_large_gain_label_written_despite_thin_evidence(session):
    # Same thin window, one snapshot at +250%: positives are never gated.
    asset, _pair, _source = _seed_asset(session, symbol="BIG", points=[(0, 1.0), (1, 3.5)])
    session.commit()
    LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_GAIN) == "2.500000"
    assert _label_value(session, asset.id, T0, LABEL_HOURS_TO_PEAK) == "1.000000"
    assert _label_value(session, asset.id, T0, LABEL_HOURS_TO_TROUGH) == "1.000000"
    assert _label_value(session, asset.id, T0, LABEL_IGNITION) == "1"


def test_generate_counts_include_forward_keys(session):
    _seed_asset(session, symbol="COUNT", points=[(h, 1.0) for h in range(30)])
    session.commit()
    counts = LabelEngine().generate(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    for key in ("forward_max_gain", "forward_max_drawdown", "hours_to_peak", "hours_to_trough"):
        assert key in counts
    assert counts["forward_max_gain"] > 0


def test_restated_snapshot_does_not_change_forward_labels(session):
    # A snapshot with observed_at AFTER the label-generation decision_ts
    # (vendor-restated history) must not create or alter forward labels.
    points = [(h, 1.5 if h == 5 else 1.0) for h in range(30)]
    asset, pair, source = _seed_asset(session, symbol="REST", points=points)
    session.commit()
    decision = T0 + timedelta(hours=48)
    LabelEngine().generate(session, decision_ts=decision)
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_GAIN) == "0.500000"
    assert _label_value(session, asset.id, T0, LABEL_HOURS_TO_PEAK) == "5.000000"

    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=T0 + timedelta(hours=3, minutes=30),
        observed_at=decision + timedelta(hours=1),
        price_usd=100.0,
        volume_usd=1_000,
        buys=10,
        sells=5,
    )
    session.commit()
    LabelEngine().generate(session, decision_ts=decision)
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_GAIN) == "0.500000"
    assert _label_value(session, asset.id, T0, LABEL_HOURS_TO_PEAK) == "5.000000"


# ── feature-aligned (bootstrap) generation ───────────────────────────────────


def test_bootstrap_writes_forward_labels(session):
    asset, _pair, _source = _seed_asset(
        session,
        symbol="BOOTF",
        points=[(h, 1.5 if h == 5 else 1.0) for h in range(30)],
    )
    upsert_feature(
        session,
        asset_id=asset.id,
        decision_ts=T0,
        feature_name="liquidity_depth",
        feature_value=200_000.0,
        source_count=1,
        freshness_score=1.0,
        missing_flag=False,
    )
    session.commit()
    counts = seed_labels_at_feature_timestamps(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_GAIN) == "0.500000"
    assert _label_value(session, asset.id, T0, LABEL_HOURS_TO_PEAK) == "5.000000"
    assert counts["forward_max_gain"] == 1


# ── data-lake generation paths ───────────────────────────────────────────────


def test_dense_generator_writes_forward_labels(session):
    from data_lake.labels import generate_dense_labels

    asset, _pair, _source = _seed_asset(
        session,
        symbol="DENSEF",
        points=[(h, 1.5 if h == 5 else 1.0) for h in range(30)],
    )
    session.commit()
    counts = generate_dense_labels(session, decision_ts=T0 + timedelta(hours=48), forward_hours=24)
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_GAIN) == "0.500000"
    assert _label_value(session, asset.id, T0, LABEL_HOURS_TO_PEAK) == "5.000000"
    assert counts["forward_max_gain"] >= 1


def test_replay_writes_forward_labels(session):
    from data_lake.labels import replay_historical_labels

    asset, _pair, _source = _seed_asset(
        session,
        symbol="REPLAYF",
        points=[(h, 1.5 if h == 5 else 1.0) for h in range(30)],
    )
    feature = upsert_feature(
        session,
        asset_id=asset.id,
        decision_ts=T0,
        feature_name="liquidity_depth",
        feature_value=200_000.0,
        source_count=1,
        freshness_score=1.0,
        missing_flag=False,
    )
    # The replay only trusts features observed by their own decision time.
    feature.observed_at = T0
    session.commit()
    counts = replay_historical_labels(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    assert _label_value(session, asset.id, T0, LABEL_FORWARD_MAX_GAIN) == "0.500000"
    assert _label_value(session, asset.id, T0, LABEL_HOURS_TO_PEAK) == "5.000000"
    assert counts["forward_max_gain_created"] == 1
