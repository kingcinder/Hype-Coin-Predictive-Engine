"""Point-in-time replay harness tests (mission-refactor Task 4).

The replay is the shared falsification instrument for the entry/exit
evaluators: it prices entries at the NEXT snapshot with market impact and
attaches causal forward paths only. These tests pin the causality contract
— nothing observed after the replay end may influence an entry, every
forward row sits inside the decision's forward window, and decision points
whose window is not fully observed are skipped, never truncated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from backtest.replay import iter_replay_entries
from storage import models
from storage.repository import (
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)


def _seed_series(
    session,
    points: list[tuple[datetime, float, datetime]],
    *,
    symbol: str = "REPLAY",
) -> models.Asset:
    """One asset/pair with snapshots at (ts, price_usd, observed_at) points."""
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"{symbol}1111111111111111111111111111111111",
        symbol=symbol,
        name=f"{symbol} Fixture",
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
    _pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=f"{symbol}Pair111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0 - timedelta(days=1),
    )
    for ts, price, observed_at in points:
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=observed_at,
            price_usd=price,
            volume_usd=10_000.0,
        )
    session.commit()
    return asset


def _hourly(
    hours: int, price_fn, *, start: datetime = T0
) -> list[tuple[datetime, float, datetime]]:
    points = []
    for h in range(hours + 1):
        ts = start + h * H
        points.append((ts, price_fn(h), ts))
    return points


def test_entry_priced_at_next_snapshot_with_impact(session):
    # Decision at 12:00 (price 1.00), next snapshot 12:05 at 1.20.
    # Entry must be 1.20 * (1 + slip) — not 1.00, not 1.20 flat.
    points = [(T0, 1.00, T0), (T0 + timedelta(minutes=5), 1.20, T0 + timedelta(minutes=5))]
    points += _hourly(24, lambda _h: 1.20, start=T0 + H)
    asset = _seed_series(session, points)
    entries = list(
        iter_replay_entries(
            session, asset_ids=[asset.id], start=T0, end=T0 + 25 * H, step_hours=25.0
        )
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.decision_ts == T0
    assert entry.entry_ts == T0 + timedelta(minutes=5)
    # Default slip 5%; the fee comes off the size, not the price.
    assert entry.entry_price == pytest.approx(1.20 * 1.05, rel=1e-6)


def test_replay_ignores_snapshots_observed_after_decision(session):
    # Clean series: 1.00 until +3h, then 1.50 flat. If the vendor-restated
    # print below leaked in, the first entry's path would peak at 50.0
    # (~+4666% over the 1.05 impact-adjusted entry) instead of 1.50.
    asset = _seed_series(session, _hourly(48, lambda h: 1.50 if h >= 3 else 1.00))
    end = T0 + 48 * H
    before = list(
        iter_replay_entries(session, asset_ids=[asset.id], start=T0, end=end, step_hours=6.0)
    )
    assert before
    assert before[0].path.max_gain_pct == pytest.approx(1.50 / 1.05 - 1, rel=1e-6)

    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=T0 + timedelta(hours=3, minutes=30),  # inside the first forward window
        observed_at=end + H,  # ...but only observed after the replay end
        price_usd=50.0,
        volume_usd=10_000.0,
    )
    session.commit()

    after = list(
        iter_replay_entries(session, asset_ids=[asset.id], start=T0, end=end, step_hours=6.0)
    )
    assert after == before


def test_feature_timestamps_precede_label_window(session):
    # Leakage audit: for every entry, entry_ts > decision_ts and every
    # forward_rows ts is > decision_ts and <= decision_ts + forward window.
    asset = _seed_series(session, _hourly(48, lambda h: 1.0 + 0.01 * h))
    entries = list(
        iter_replay_entries(
            session, asset_ids=[asset.id], start=T0, end=T0 + 48 * H, step_hours=3.0
        )
    )
    assert entries
    for entry in entries:
        assert entry.entry_ts > entry.decision_ts
        assert all(
            entry.decision_ts < ts <= entry.decision_ts + timedelta(hours=24)
            for ts, _ in entry.forward_rows
        )


def test_forward_path_attached_from_entry_price(session):
    # Entry at +1h (1.00 -> 1.05 impact-adjusted); peak 2.10 at +5h,
    # trough 0.525 at +9h. Path gains are measured off the price actually
    # paid, and hours are measured from the decision (the ForwardPath
    # convention the labels use).
    def price(h: int) -> float:
        if h == 5:
            return 2.10
        if h == 9:
            return 0.525
        return 1.00

    asset = _seed_series(session, _hourly(30, price))
    entries = list(
        iter_replay_entries(
            session, asset_ids=[asset.id], start=T0, end=T0 + 30 * H, step_hours=30.0
        )
    )
    assert len(entries) == 1
    path = entries[0].path
    assert path.max_gain_pct == pytest.approx(2.10 / 1.05 - 1, rel=1e-6)
    assert path.max_drawdown_pct == pytest.approx(0.525 / 1.05 - 1, rel=1e-6)
    assert path.hours_to_peak == pytest.approx(5.0)
    assert path.hours_to_trough == pytest.approx(9.0)


def test_decisions_without_a_full_forward_window_are_skipped(session):
    asset = _seed_series(session, _hourly(29, lambda _h: 1.00))
    end = T0 + 29 * H
    entries = list(
        iter_replay_entries(session, asset_ids=[asset.id], start=T0, end=end, step_hours=1.0)
    )
    # Entry lands one hour after the decision, so the last decision whose
    # entry window (entry_ts + 24h) fits inside the replay is T0+4h.
    assert {entry.decision_ts for entry in entries} == {T0 + h * H for h in range(5)}


def test_entry_without_any_forward_rows_is_skipped(session):
    # The entry snapshot is the last print: its window fits before `end`,
    # but there is nothing after entry to build a path from — the entry is
    # skipped, not emitted with an empty/fabricated path.
    asset = _seed_series(session, [(T0, 1.00, T0), (T0 + H, 1.10, T0 + H)])
    entries = list(
        iter_replay_entries(
            session, asset_ids=[asset.id], start=T0, end=T0 + 30 * H, step_hours=30.0
        )
    )
    assert entries == []
