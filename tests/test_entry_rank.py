"""Entry rank composite + falsification tests (mission-refactor Task 5).

The composite is the validated Ticket 1 rank-sum trio
(``unique_buyers_estimate``, ``buy_sell_ratio``, ``liquidity_depth``) plus
the new ``net_inflow_ratio``, percentile-ranked within the day cohort and
summed — with a wash-lite adjustment that damps ``buy_sell_ratio`` when
volume-per-buyer screams wash trading. The falsification instrument
replays history and demands the composite's precision@top-20/day for
MFE >= 2x within 6h beat a volume-only ranking by >= 1.5x; these tests
exercise it on synthetic fixtures only (the live run is gated on the
Task 0 snapshot-coverage precondition).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from backtest.replay import ReplayEntry
from common.config import get_settings
from features.factory import FeatureValue
from forecast.labels import ForwardPath
from scoring.entry_rank import (
    ENTRY_RANK_COMPONENTS,
    rank_entry_cohort,
    wash_lite_adjust,
)
from scripts import falsify_entry_rank
from scripts.falsify_entry_rank import evaluate_cohorts
from storage.repository import insert_market_snapshot_once, upsert_asset, upsert_pool_and_pair
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
H = timedelta(hours=1)
DAY = timedelta(days=1)


# ---------------------------------------------------------------------------
# wash_lite_adjust
# ---------------------------------------------------------------------------


def test_wash_lite_flags_and_downweights():
    # intensity = 400 / 10 = 40 > 20 threshold; buy_sell_ratio 3.0 ->
    # 1.0 + (3.0 - 1.0) * 0.5 = 2.0.
    feats = {"buy_sell_ratio": 3.0, "unique_buyers_estimate": 10.0, "volume_acceleration": 400.0}
    adjusted, flagged = wash_lite_adjust(feats, settings=get_settings())
    assert flagged is True
    assert adjusted["buy_sell_ratio"] == pytest.approx(2.0)


def test_wash_lite_below_threshold_unflagged_and_input_not_mutated():
    feats = {"buy_sell_ratio": 3.0, "unique_buyers_estimate": 10.0, "volume_acceleration": 100.0}
    adjusted, flagged = wash_lite_adjust(feats, settings=get_settings())
    assert flagged is False
    assert adjusted["buy_sell_ratio"] == pytest.approx(3.0)
    assert feats["buy_sell_ratio"] == pytest.approx(3.0)


def test_wash_lite_missing_inputs_unflagged():
    adjusted, flagged = wash_lite_adjust({"buy_sell_ratio": 3.0}, settings=get_settings())
    assert flagged is False
    assert adjusted == {"buy_sell_ratio": 3.0}


# ---------------------------------------------------------------------------
# rank_entry_cohort
# ---------------------------------------------------------------------------


def _full_row(base: float, volume: float) -> dict[str, float]:
    row = {name: base for name in ENTRY_RANK_COMPONENTS}
    row["volume_acceleration"] = volume
    return row


def test_rank_entry_cohort_missing_component_scores_zero():
    rows = [
        (1, _full_row(10.0, 5.0)),
        (2, {name: 20.0 for name in ENTRY_RANK_COMPONENTS if name != "net_inflow_ratio"}),
    ]
    candidates = rank_entry_cohort(rows, settings=get_settings())
    by_id = {candidate.asset_id: candidate for candidate in candidates}
    assert by_id[2].components["net_inflow_ratio"] == 0.0  # absent, not 0.5


def test_rank_entry_cohort_percentiles_and_order():
    rows = [(1, _full_row(10.0, 5.0)), (2, _full_row(20.0, 5.0)), (3, _full_row(30.0, 5.0))]
    candidates = rank_entry_cohort(rows, settings=get_settings())
    assert [candidate.asset_id for candidate in candidates] == [3, 2, 1]
    by_id = {candidate.asset_id: candidate for candidate in candidates}
    # Midrank percentiles for values 10/20/30 in a cohort of 3: 1/6, 1/2, 5/6
    # per component; rank_sum is the sum over the four components.
    assert by_id[3].rank_sum == pytest.approx(4 * 5 / 6)
    assert by_id[2].rank_sum == pytest.approx(4 * 0.5)
    assert by_id[1].rank_sum == pytest.approx(4 * 1 / 6)


def test_rank_entry_cohort_wash_flag_damps_buy_sell_ratio():
    # Asset 1's raw buy_sell_ratio (3.0) beats asset 2's (2.5), but asset 1
    # is wash-flagged (intensity 40 > 20), damping it to 2.0 — so asset 2
    # must win the buy_sell_ratio percentile and asset 1 carries the flag.
    wash_row = {
        "net_inflow_ratio": 0.1,
        "buy_sell_ratio": 3.0,
        "unique_buyers_estimate": 10.0,
        "liquidity_depth": 100.0,
        "volume_acceleration": 400.0,
    }
    clean_row = {
        "net_inflow_ratio": 0.1,
        "buy_sell_ratio": 2.5,
        "unique_buyers_estimate": 10.0,
        "liquidity_depth": 100.0,
        "volume_acceleration": 100.0,
    }
    candidates = rank_entry_cohort([(1, wash_row), (2, clean_row)], settings=get_settings())
    by_id = {candidate.asset_id: candidate for candidate in candidates}
    assert by_id[1].wash_flagged is True
    assert by_id[2].wash_flagged is False
    assert by_id[2].components["buy_sell_ratio"] > by_id[1].components["buy_sell_ratio"]


# ---------------------------------------------------------------------------
# falsification: pure evaluation over synthetic cohorts
# ---------------------------------------------------------------------------


def _entry(asset_id: int, decision: datetime, *, outcome: str) -> ReplayEntry:
    entry_ts = decision + H
    if outcome == "hit":
        rows = ((entry_ts + H, 1.5), (entry_ts + 3 * H, 2.5))
        gain = 1.5
    elif outcome == "late_hit":
        rows = ((entry_ts + H, 1.5), (entry_ts + 8 * H, 2.5))
        gain = 1.5
    else:
        rows = ((entry_ts + H, 1.5), (entry_ts + 3 * H, 1.8))
        gain = 0.8
    return ReplayEntry(
        asset_id=asset_id,
        decision_ts=decision,
        entry_ts=entry_ts,
        entry_price=1.0,
        path=ForwardPath(
            max_gain_pct=gain, max_drawdown_pct=-0.1, hours_to_peak=3.0, hours_to_trough=1.0
        ),
        forward_rows=rows,
    )


def _cohort(
    day: datetime, *, hits: int, misses: int, composite_predicts: bool
) -> list[tuple[ReplayEntry, dict[str, float]]]:
    """One day cohort: hits get high composite components iff
    ``composite_predicts``; volume always ranks the misses first."""
    cohort = []
    total = hits + misses
    for idx in range(total):
        asset_id = idx + 1
        is_hit = idx < hits
        if composite_predicts:
            quality = float(total - idx)  # hits (first idx) rank top
        else:
            quality = float(idx + 1)  # misses rank top on the composite too
        row = {name: quality for name in ENTRY_RANK_COMPONENTS}
        row["unique_buyers_estimate"] = 100.0  # wash intensity stays low
        row["volume_acceleration"] = float(idx + 1)  # misses (later idx) top
        cohort.append((_entry(asset_id, day, outcome="hit" if is_hit else "miss"), row))
    return cohort


def test_falsification_passes_when_composite_beats_volume():
    cohorts = {date(2026, 5, 1): _cohort(T0, hits=12, misses=14, composite_predicts=True)}
    report = evaluate_cohorts(
        cohorts, settings=get_settings(), window_start=T0, window_end=T0 + DAY
    )
    # Composite top-20: 12 hits + 8 misses -> 0.6. Volume top-20: the 14
    # misses first, then 6 hits -> 0.3. Lift 2.0 >= 1.5x kill bar.
    assert report.composite_precision == pytest.approx(0.6)
    assert report.volume_precision == pytest.approx(0.3)
    assert report.lift == pytest.approx(2.0)
    assert report.passed is True
    # BH screening block is quoted across exactly the screened components.
    assert {screen.name for screen in report.screening} == set(ENTRY_RANK_COMPONENTS)
    assert all(0.0 <= screen.q_value <= 1.0 for screen in report.screening)


def test_falsification_fails_kill_bar_when_composite_tracks_volume():
    cohorts = {date(2026, 5, 1): _cohort(T0, hits=12, misses=14, composite_predicts=False)}
    report = evaluate_cohorts(
        cohorts, settings=get_settings(), window_start=T0, window_end=T0 + DAY
    )
    assert report.lift == pytest.approx(1.0)
    assert report.passed is False


def test_falsification_late_hit_beyond_6h_does_not_count():
    cohort = [(_entry(1, T0, outcome="late_hit"), _full_row(10.0, 5.0))]
    report = evaluate_cohorts(
        {date(2026, 5, 1): cohort}, settings=get_settings(), window_start=T0, window_end=T0 + DAY
    )
    assert report.composite_precision == 0.0
    assert report.passed is False


# ---------------------------------------------------------------------------
# falsification: end-to-end on a seeded synthetic DB (report either way)
# ---------------------------------------------------------------------------


def _seed_price_series(session, *, symbol: str, spiky: bool) -> None:
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"{symbol}1111111111111111111111111111111111",
        symbol=symbol,
        name=f"{symbol} Fixture",
        first_seen_at=T0 - DAY,
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="USDC111111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=T0 - 365 * DAY,
    )
    _pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=f"{symbol}Pair111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0 - DAY,
    )
    for hour in range(4 * 24 + 1):
        ts = T0 + hour * H
        price = 3.0 if (spiky and hour % 24 == 4) else 1.0
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=10_000.0,
        )
    session.commit()


def test_falsification_run_writes_report_when_kill_bar_fails(session, tmp_path, monkeypatch):
    _seed_price_series(session, symbol="SPIKY", spiky=True)
    _seed_price_series(session, symbol="FLATB", spiky=False)
    _seed_price_series(session, symbol="FLATC", spiky=False)

    def _fake_features(_session, *, decision_ts, asset_ids):
        # Identical features for every asset: the composite cannot outrank
        # the volume baseline, so the kill bar must fail — and the report
        # must still be written.
        return {
            asset_id: {
                name: FeatureValue(name=name, value=1.0, missing=False)
                for name in (*ENTRY_RANK_COMPONENTS, "volume_acceleration")
            }
            for asset_id in asset_ids
        }

    monkeypatch.setattr(falsify_entry_rank, "build_replay_features", _fake_features)
    exit_code = falsify_entry_rank.run_falsification(
        session=session,
        settings=get_settings(),
        as_of=T0 + 4 * DAY,
        days=4,
        reports_dir=tmp_path,
    )
    assert exit_code == 1
    # The report stamp is the as_of date, not the window start.
    json_path = tmp_path / f"entry-rank-falsification-{(T0 + 4 * DAY).strftime('%Y%m%d')}.json"
    md_path = json_path.with_suffix(".md")
    assert json_path.exists()
    assert md_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["days"], "expected at least one fully-observed day cohort"
    assert {screen["name"] for screen in payload["screening"]} == set(ENTRY_RANK_COMPONENTS)
