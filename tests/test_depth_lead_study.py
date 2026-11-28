"""Depth-lead study tests (mission-refactor Task 15, spec §4 build order 1).

The pacing premise under test: quote depth peaks BEFORE the
executable-value peak, so Task 9's depth-paced exit ladder would be
reading a leading indicator rather than steering by a mirror. The lead
statistic is exercised on synthetic episodes (depth-first, EV-first,
simultaneous peaks), and the study runner on a seeded fixture DB only —
the live run is a separate, recorded step (STATE.md), never fabricated
from fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from common.config import get_settings
from forecast.labels import LABEL_EXECUTABLE_PEAK_GAIN_S10, upsert_label
from scripts import depth_lead_study
from scripts.depth_lead_study import EpisodeLead, episode_lead, median_lead
from storage.repository import (
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0)  # naive for SQLite compatibility
START = datetime(2026, 11, 20, tzinfo=UTC)


def _rows(points: list[tuple[float, float, float | None]]):
    """(hour offset, price, quote depth USD) -> harvest rows."""
    return [(START + timedelta(hours=h), price, depth) for h, price, depth in points]


# ── the lead statistic ───────────────────────────────────────────────────────


def test_episode_lead_positive_when_depth_peaks_first():
    # Depth peaks at row 2 (1_000); price stays 1.0 until row 5 (1.2),
    # so the f=0.10 executable value (N = 0.10 * 1_000 = 100) peaks at
    # row 5: EV row 5 = 1.2 * 700/800 = 1.05 > row 2's 1_000/1_100.
    rows = _rows(
        [
            (0, 1.0, 100),
            (1, 1.0, 200),
            (2, 1.0, 1_000),
            (3, 1.0, 900),
            (4, 1.0, 800),
            (5, 1.2, 700),
            (6, 1.1, 600),
        ]
    )
    lead = episode_lead(rows, asset_id=7, settings=get_settings())
    assert lead is not None
    assert lead.asset_id == 7
    assert lead.depth_peak_ts == START + timedelta(hours=2)
    assert lead.ev_peak_ts == START + timedelta(hours=5)
    assert lead.lead_snapshots == 3
    assert lead.lead_hours == 3.0


def test_episode_lead_negative_when_depth_trails_ev():
    # Depth rises through the whole episode (peak at the last row)
    # while price falls from the open: EV peaks at row 0
    # (2.0 * 100/160 = 1.25) and depth only peaks at row 5.
    rows = _rows(
        [
            (0, 2.0, 100),
            (1, 1.5, 200),
            (2, 1.2, 300),
            (3, 1.0, 400),
            (4, 0.9, 500),
            (5, 0.85, 600),
        ]
    )
    lead = episode_lead(rows, asset_id=7, settings=get_settings())
    assert lead is not None
    assert lead.lead_snapshots == -5
    assert lead.lead_snapshots < 0
    assert lead.lead_hours == -5.0


def test_episode_lead_zero_when_peaks_simultaneous():
    # Flat price: EV is maximized where depth is, so both peaks sit on
    # row 1 and the lead is exactly zero — not evidence for pacing.
    rows = _rows([(0, 1.0, 100), (1, 1.0, 500), (2, 1.0, 300)])
    lead = episode_lead(rows, asset_id=7, settings=get_settings())
    assert lead is not None
    assert lead.lead_snapshots == 0
    assert lead.lead_hours == 0.0


def test_episode_lead_skips_unusable_depth_but_counts_its_index():
    # Row 0 has no depth print: it cannot host either peak, but it is
    # still a snapshot of the episode, so the peaks' indices (and the
    # lead) are counted in the full row sequence. Depth peaks at row 2,
    # EV at row 4 (1.3 * 800/900 = 1.156 > 1_000/1_100 at row 2).
    rows = _rows(
        [
            (0, 1.0, None),
            (1, 1.0, 500),
            (2, 1.0, 1_000),
            (3, 1.0, 900),
            (4, 1.3, 800),
            (5, 1.0, 700),
        ]
    )
    lead = episode_lead(rows, asset_id=7, settings=get_settings())
    assert lead is not None
    assert lead.lead_snapshots == 2
    assert lead.lead_hours == 2.0


def test_episode_lead_none_without_usable_depth():
    assert episode_lead([], asset_id=7, settings=get_settings()) is None
    rows = _rows([(0, 1.0, None), (1, 2.0, 0), (2, 3.0, -5)])
    assert episode_lead(rows, asset_id=7, settings=get_settings()) is None


def test_median_lead_uses_episode_median_not_mean():
    # One outlier episode with lead +40 must not carry the median:
    # median of [2, 3, 40] is 3; the mean would be 15.
    leads = [
        EpisodeLead(
            asset_id=asset_id,
            depth_peak_ts=START,
            ev_peak_ts=START,
            lead_snapshots=snapshots,
            lead_hours=float(snapshots),
        )
        for asset_id, snapshots in enumerate([2, 3, 40], start=1)
    ]
    assert median_lead(leads) == 3.0


def test_median_lead_empty_is_zero():
    # No episodes is no evidence of a lead; 0.0 fails the > 0 gate,
    # which is the conservative direction for Task 9's default.
    assert median_lead([]) == 0.0


# ── the study runner on a seeded fixture DB ──────────────────────────────────


def _seed_episode(session, *, symbol: str, s10_gain: str):
    """One asset, hourly prints for 25h, and a Task 7 s10 label at T0.

    Depth peaks at +2h (10_000) and decays; price is 1.0 except a 2.0
    print at +5h. At the f=0.10 reference (N = 1_000) the executable
    peak is +5h (2.0 * 7_000/8_000 = 1.75, gain 0.75 — the seeded label
    value), three snapshots after the depth peak.
    """
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
    pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=f"Pair{symbol}1111111111111111111111111111111111",
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=T0,
    )
    depths = {1: 5_000, 2: 10_000, 3: 9_000, 4: 8_000, 5: 7_000, 6: 6_000}
    for hour in range(25):
        ts = T0 + timedelta(hours=hour)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=2.0 if hour == 5 else 1.0,
            volume_usd=1_000,
            buys=10,
            sells=5,
        )
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            reserve_usd=depths.get(hour, 5_000),
        )
    upsert_label(
        session,
        asset_id=asset.id,
        ts=T0,
        label_type=LABEL_EXECUTABLE_PEAK_GAIN_S10,
        label_value=s10_gain,
        decision_ts=T0 + timedelta(hours=48),
        source="forecast:test",
    )
    session.commit()
    return asset


def test_run_study_reports_median_lead_and_passes_gate(session, tmp_path):
    asset = _seed_episode(session, symbol="LEAD", s10_gain="0.750000")
    # A zero-gain s10 label is not an executable peak: not an episode.
    _seed_episode(session, symbol="FLAT", s10_gain="0.000000")
    as_of = T0 + timedelta(hours=72)
    code = depth_lead_study.run_study(
        session=session,
        settings=get_settings(),
        as_of=as_of,
        days=30,
        reports_dir=tmp_path,
    )
    assert code == 0
    payload = json.loads(
        (tmp_path / f"depth-lead-study-{as_of.strftime('%Y%m%d')}.json").read_text(encoding="utf-8")
    )
    assert payload["n_episodes"] == 1
    assert payload["median_lead_snapshots"] == 3.0
    assert payload["median_lead_hours"] == 3.0
    assert payload["share_lead_positive"] == 1.0
    assert payload["passed"] is True
    (episode,) = payload["episodes"]
    assert episode["asset_id"] == asset.id
    assert episode["lead_snapshots"] == 3
    assert episode["lead_hours"] == 3.0
    assert (tmp_path / f"depth-lead-study-{as_of.strftime('%Y%m%d')}.md").exists()


def test_run_study_no_episodes_exits_1_and_writes_report(session, tmp_path):
    as_of = T0 + timedelta(hours=72)
    code = depth_lead_study.run_study(
        session=session,
        settings=get_settings(),
        as_of=as_of,
        days=30,
        reports_dir=tmp_path,
    )
    assert code == 1
    payload = json.loads(
        (tmp_path / f"depth-lead-study-{as_of.strftime('%Y%m%d')}.json").read_text(encoding="utf-8")
    )
    assert payload["n_episodes"] == 0
    assert payload["median_lead_snapshots"] == 0.0
    assert payload["median_lead_hours"] is None
    assert payload["passed"] is False
