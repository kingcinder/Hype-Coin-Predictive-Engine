"""Distribution-ensemble feature tests (mission-refactor Task 16).

Spec §4 layer 3 — distribution forensics. The Step 1 verification
(``reports/holder-delta-verification-20261128.md``) found Branch A:
per-wallet balances ARE stored per (asset, wallet, ts) snapshot by
both holder ingest paths, so balance deltas are reconstructible
point-in-time where snapshot density and wallet continuity suffice —
and read missing, never fabricated, where they do not.

Features pinned here:

- ``top_holder_net_flow`` — sum of Δbalance over the top-10 holders
  by balance at decision time, differenced against the immediately
  preceding snapshot; negative = distribution. Missing when fewer
  than half the top 10 have a prior-snapshot row to difference.
- ``effort_vs_result_depth_normalized`` — volume (effort) against
  shrinking price response (result), the response normalized by
  quote depth; rises on the distribution pattern.
- ``holder_stall`` — 1.0 when holder-count growth is ≈ 0 while
  top-10 concentration rises, else 0.0, from ``holder_metrics``.

Votes: ``distribution_votes`` derives the three accelerate votes the
exit policy consumes (≥ 2 of 3 active accelerates an ARMED ladder).
The derivation returns votes only — it has no exit action to fire,
so votes alone can never sell anything; the armed-gating itself is
``ExitPolicy`` behaviour (Task 9, ``trading/exit_policy.py``, not
yet in this tree — the policy-side test lands with that task).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from features.factory import (
    DISTRIBUTION_EVR_VOTE_THRESHOLD,
    FeatureFactory,
    FeatureValue,
    distribution_votes,
)
from storage import models
from storage.repository import (
    insert_holder_once,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)


def _seed(session) -> tuple[models.Asset, models.Pool, models.Pair, models.Source]:
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Dist1111111111111111111111111111111111111",
        symbol="DIST",
        name="Distribution Fixture",
        first_seen_at=T0 - timedelta(days=1),
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="USDC111111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=T0 - timedelta(days=365),
    )
    pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address="DistPair11111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0 - timedelta(days=1),
    )
    pool.created_at = T0 - timedelta(days=1)
    pair.created_at = T0 - timedelta(days=1)
    session.commit()
    return asset, pool, pair, source


def _holder(
    session,
    asset: models.Asset,
    source: models.Source,
    wallet: str,
    *,
    ts: datetime,
    balance: float,
    observed_at: datetime | None = None,
) -> None:
    insert_holder_once(
        session,
        asset_id=asset.id,
        wallet_address=wallet,
        source_id=source.id,
        ts=ts,
        observed_at=observed_at or ts,
        balance=balance,
        pct_supply=None,
    )
    session.commit()


def _metric(
    session,
    asset: models.Asset,
    *,
    ts: datetime,
    holder_count: int | None,
    top10_pct: float | None,
    growth: float | None = None,
) -> None:
    session.add(
        models.HolderMetric(
            asset_id=asset.id,
            ts=ts,
            top10_pct=None if top10_pct is None else Decimal(str(top10_pct)),
            holder_count=holder_count,
            holder_growth_per_hour=None if growth is None else Decimal(str(growth)),
        )
    )
    session.commit()


def _features(session, asset: models.Asset, decision_ts: datetime) -> dict[str, FeatureValue]:
    values = FeatureFactory().build_for_asset(session, asset, decision_ts)
    return {value.name: value for value in values}


def _two_snapshots_top10_sell(session, asset, source) -> None:
    # 12 wallets, balances descending 1200..100 at T0-2h; at T0-1h the
    # top 10 have each sold 50 while the tail is unchanged.
    for i in range(12):
        _holder(session, asset, source, f"w{i}", ts=T0 - 2 * H, balance=1200.0 - 100 * i)
    for i in range(12):
        delta = -50.0 if i < 10 else 0.0
        _holder(session, asset, source, f"w{i}", ts=T0 - 1 * H, balance=1200.0 - 100 * i + delta)


def test_top_holder_net_flow_negative_when_top10_sell(session):
    asset, _pool, _pair, source = _seed(session)
    _two_snapshots_top10_sell(session, asset, source)

    feats = _features(session, asset, T0)
    flow = feats["top_holder_net_flow"]
    assert flow.missing is False
    assert flow.value == pytest.approx(-500.0)
    assert flow.value < 0.0


def test_top_holder_net_flow_positive_when_top10_accumulate(session):
    asset, _pool, _pair, source = _seed(session)
    for i in range(12):
        _holder(session, asset, source, f"w{i}", ts=T0 - 2 * H, balance=1200.0 - 100 * i)
        _holder(
            session,
            asset,
            source,
            f"w{i}",
            ts=T0 - 1 * H,
            balance=1200.0 - 100 * i + (25.0 if i < 10 else 0.0),
        )

    feats = _features(session, asset, T0)
    assert feats["top_holder_net_flow"].value == pytest.approx(250.0)


def test_top_holder_net_flow_missing_when_coverage_thin(session):
    # The wallet set churns: only 3 of the decision-time top 10 exist
    # in the prior snapshot — a minority sum would misread as a flow,
    # so the feature is missing, never fabricated.
    asset, _pool, _pair, source = _seed(session)
    for i in range(12):
        _holder(session, asset, source, f"old{i}", ts=T0 - 2 * H, balance=500.0)
    for i in range(12):
        wallet = f"old{i}" if i < 3 else f"new{i}"
        _holder(session, asset, source, wallet, ts=T0 - 1 * H, balance=1200.0 - 100 * i)

    feats = _features(session, asset, T0)
    assert feats["top_holder_net_flow"].missing is True


def test_top_holder_net_flow_missing_with_single_snapshot(session):
    asset, _pool, _pair, source = _seed(session)
    for i in range(12):
        _holder(session, asset, source, f"w{i}", ts=T0 - 1 * H, balance=1200.0 - 100 * i)

    feats = _features(session, asset, T0)
    assert feats["top_holder_net_flow"].missing is True


def test_top_holder_net_flow_ignores_post_decision_evidence(session):
    # Point-in-time: a snapshot after the decision (a later dump) and
    # a restated row whose ts precedes the decision but which was
    # only observed after it must not move the value at decision time.
    asset, _pool, _pair, source = _seed(session)
    _two_snapshots_top10_sell(session, asset, source)
    for i in range(12):
        _holder(session, asset, source, f"w{i}", ts=T0 + H, balance=10.0)
    _holder(
        session,
        asset,
        source,
        "w0",
        ts=T0 - 30 * timedelta(minutes=1),
        balance=999_999.0,
        observed_at=T0 + 2 * H,
    )

    feats = _features(session, asset, T0)
    assert feats["top_holder_net_flow"].value == pytest.approx(-500.0)


def _market_prints(session, pair, source, *, volumes: list[float], prices: list[float]) -> None:
    # 12 hourly prints ending at T0, plus flat quote depth per print.
    start = T0 - 11 * H
    for k, (volume, price) in enumerate(zip(volumes, prices, strict=True)):
        ts = start + k * H
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=volume,
        )
    session.commit()


def _depth_prints(session, pool, source, *, reserve_usd: float = 100_000.0) -> None:
    for k in range(12):
        ts = T0 - 11 * H + k * H
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            reserve_usd=reserve_usd,
        )
    session.commit()


def test_effort_vs_result_rises_on_distribution_pattern(session):
    asset, pool, pair, source = _seed(session)
    # Distribution: recent-half volume 3x the earlier half, while the
    # price response per print collapses (1.00->1.60 earlier on the
    # same effort scale, only 1.60->1.66 recently), depth flat.
    _market_prints(
        session,
        pair,
        source,
        volumes=[100.0] * 6 + [300.0] * 6,
        prices=[1.00, 1.12, 1.24, 1.36, 1.48, 1.60, 1.60, 1.612, 1.624, 1.636, 1.648, 1.66],
    )
    _depth_prints(session, pool, source)

    feats = _features(session, asset, T0)
    evr = feats["effort_vs_result_depth_normalized"]
    assert evr.missing is False
    assert evr.value > DISTRIBUTION_EVR_VOTE_THRESHOLD


def test_effort_vs_result_neutral_on_healthy_pattern(session):
    asset, pool, pair, source = _seed(session)
    # Healthy: flat volume, identical price response in both halves.
    _market_prints(
        session,
        pair,
        source,
        volumes=[100.0] * 12,
        prices=[1.00, 1.12, 1.24, 1.36, 1.48, 1.60, 1.60, 1.72, 1.84, 1.96, 2.08, 2.20],
    )
    _depth_prints(session, pool, source)

    feats = _features(session, asset, T0)
    evr = feats["effort_vs_result_depth_normalized"]
    assert evr.missing is False
    assert evr.value == pytest.approx(1.0)
    assert evr.value <= DISTRIBUTION_EVR_VOTE_THRESHOLD


def test_effort_vs_result_missing_with_too_few_prints(session):
    asset, pool, pair, source = _seed(session)
    _market_prints(session, pair, source, volumes=[100.0] * 3, prices=[1.0, 1.1, 1.2])
    _depth_prints(session, pool, source)

    feats = _features(session, asset, T0)
    assert feats["effort_vs_result_depth_normalized"].missing is True


def test_holder_stall_active_on_flat_count_rising_concentration(session):
    asset, _pool, _pair, _source = _seed(session)
    _metric(session, asset, ts=T0 - 3 * H, holder_count=1000, top10_pct=0.40)
    _metric(session, asset, ts=T0 - 1 * H, holder_count=1000, top10_pct=0.45)

    feats = _features(session, asset, T0)
    stall = feats["holder_stall"]
    assert stall.missing is False
    assert stall.value == 1.0


def test_holder_stall_inactive_when_count_grows(session):
    asset, _pool, _pair, _source = _seed(session)
    _metric(session, asset, ts=T0 - 3 * H, holder_count=1000, top10_pct=0.40)
    _metric(session, asset, ts=T0 - 1 * H, holder_count=1200, top10_pct=0.45)

    feats = _features(session, asset, T0)
    assert feats["holder_stall"].value == 0.0


def test_holder_stall_inactive_when_concentration_falls(session):
    asset, _pool, _pair, _source = _seed(session)
    _metric(session, asset, ts=T0 - 3 * H, holder_count=1000, top10_pct=0.45)
    _metric(session, asset, ts=T0 - 1 * H, holder_count=1000, top10_pct=0.40)

    feats = _features(session, asset, T0)
    assert feats["holder_stall"].value == 0.0


def test_holder_stall_missing_without_metric_pair(session):
    asset, _pool, _pair, _source = _seed(session)
    _metric(session, asset, ts=T0 - 1 * H, holder_count=1000, top10_pct=0.45)

    feats = _features(session, asset, T0)
    assert feats["holder_stall"].missing is True


def _fv(name: str, value: float | None) -> FeatureValue:
    if value is None:
        return FeatureValue(name=name, value=0.0, missing=True)
    return FeatureValue(name=name, value=value, missing=False)


def test_accelerate_votes_require_two_of_three():
    # Two active votes -> accelerates; one -> does not; none -> does not.
    two = distribution_votes(
        {
            "effort_vs_result_depth_normalized": _fv("effort_vs_result_depth_normalized", None),
            "top_holder_net_flow": _fv("top_holder_net_flow", -500.0),
            "holder_stall": _fv("holder_stall", 1.0),
        }
    )
    assert two.top_holder_net_flow is True
    assert two.holder_stall is True
    assert two.effort_vs_result is False
    assert two.active_count == 2
    assert two.accelerates is True

    one = distribution_votes(
        {
            "effort_vs_result_depth_normalized": _fv("effort_vs_result_depth_normalized", 3.0),
            "top_holder_net_flow": _fv("top_holder_net_flow", 250.0),
            "holder_stall": _fv("holder_stall", 0.0),
        }
    )
    assert one.active_count == 1
    assert one.accelerates is False

    none = distribution_votes({})
    assert none.active_count == 0
    assert none.accelerates is False


def test_votes_are_neutral_when_features_missing():
    # Missing evidence is a neutral vote, never an active one — the
    # ensemble degrades to the layers under it (spec §4 discipline).
    votes = distribution_votes(
        {
            "effort_vs_result_depth_normalized": _fv("effort_vs_result_depth_normalized", None),
            "top_holder_net_flow": _fv("top_holder_net_flow", None),
            "holder_stall": _fv("holder_stall", None),
        }
    )
    assert votes.active_count == 0
    assert votes.accelerates is False
