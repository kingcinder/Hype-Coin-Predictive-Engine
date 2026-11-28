"""Smart-money convergence case-control gate tests (mission-refactor Task 12a).

The gate (spec §3 item 5) asks whether point-in-time "repeat winner"
convergence predicts pumps: a decision point is *converged* when at
least ``k`` distinct wallets holding the asset in the 6h before the
decision were also early holders of >= 2 *prior* cases (points whose
``forward_max_gain_pct`` reached >= 1.0). The winner set is built
strictly from data observed before each decision — a wallet whose
first case-win lands after the decision point must not count for that
point (the plan's pinning test below). The gate passes iff the pump
share among converged points is >= 3x the pump share among matched
controls (same day, same liquidity-depth decile) with n >= 30
converged points. 12b (the wallet-PnL table + feature) builds only on
a pass; these tests exercise the statistic on seeded fixtures only —
the live run is a deployment step (no copy of the live DB exists on
this VM).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from forecast.labels import LABEL_FORWARD_MAX_GAIN, upsert_label
from scripts import case_control_smart_money
from scripts.case_control_smart_money import (
    convergence_stats,
    evaluate_points,
    run_case_control,
)
from storage import models
from storage.repository import (
    insert_holder_once,
    insert_liquidity_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
H = timedelta(hours=1)
DAY = timedelta(days=1)

WINNERS = ("WalletA", "WalletB", "WalletW")


def _asset(session, chain_id: int, tag: str) -> int:
    asset = upsert_asset(
        session,
        chain_id=chain_id,
        address=f"{tag}Address",
        symbol=tag[:8].upper(),
        name=f"{tag} Fixture",
        first_seen_at=T0,
    )
    session.flush()
    return int(asset.id)


def _point(session, *, asset_id: int, ts: datetime, gain: float) -> None:
    upsert_label(
        session,
        asset_id=asset_id,
        ts=ts,
        label_type=LABEL_FORWARD_MAX_GAIN,
        label_value=f"{gain:.6f}",
        decision_ts=ts,
        source="forecast:test",
    )


def _holders(
    session,
    *,
    asset_id: int,
    source_id: int,
    ts: datetime,
    wallets: tuple[str, ...],
    observed_at: datetime | None = None,
) -> None:
    for wallet in wallets:
        insert_holder_once(
            session,
            asset_id=asset_id,
            wallet_address=wallet,
            source_id=source_id,
            ts=ts,
            observed_at=observed_at if observed_at is not None else ts,
            balance=100.0,
            pct_supply=None,
        )


def _case(
    session,
    *,
    chain_id: int,
    source_id: int,
    tag: str,
    ts: datetime,
    wallets: tuple[str, ...],
) -> int:
    """A pumped decision point (a case) early-held by ``wallets``."""
    asset_id = _asset(session, chain_id, tag)
    _point(session, asset_id=asset_id, ts=ts, gain=1.5)
    _holders(session, asset_id=asset_id, source_id=source_id, ts=ts - H, wallets=wallets)
    return asset_id


def _seed_repeat_winners(session, *, chain_id: int, source_id: int) -> None:
    """Two prior cases (days 1 and 2) early-held by WINNERS: afterwards
    each of them is a repeat winner (>= 2 prior cases)."""
    _case(
        session,
        chain_id=chain_id,
        source_id=source_id,
        tag="CaseOne",
        ts=T0 + DAY,
        wallets=WINNERS,
    )
    _case(
        session,
        chain_id=chain_id,
        source_id=source_id,
        tag="CaseTwo",
        ts=T0 + 2 * DAY,
        wallets=WINNERS,
    )
    session.commit()


def _by_asset(evaluations) -> dict[int, object]:
    return {ev.asset_id: ev for ev in evaluations}


# ---------------------------------------------------------------------------
# Point-in-time discipline (the plan's pinning test)
# ---------------------------------------------------------------------------


def test_case_control_statistic_counts_only_prior_winners(session):
    """A wallet whose first case-win is AFTER the decision point must
    not count as a repeat winner for that point."""
    chain, source = seed_reference(session)

    # Point P0 sits BEFORE both cases; the same wallets hold it early.
    early_id = _asset(session, chain.id, "EarlyPoint")
    _point(session, asset_id=early_id, ts=T0, gain=0.2)
    _holders(
        session,
        asset_id=early_id,
        source_id=source.id,
        ts=T0 - H,
        wallets=(*WINNERS, "WalletLate"),
    )

    # Case 1 (day 1): WINNERS hold early (inside the 6h window);
    # WalletLate's row sits 7h before the decision — outside the
    # window, so this case must not count for WalletLate.
    case1_id = _asset(session, chain.id, "CaseOne")
    _point(session, asset_id=case1_id, ts=T0 + DAY, gain=1.5)
    _holders(
        session,
        asset_id=case1_id,
        source_id=source.id,
        ts=T0 + DAY - H,
        wallets=WINNERS,
    )
    _holders(
        session,
        asset_id=case1_id,
        source_id=source.id,
        ts=T0 + DAY - 7 * H,
        wallets=("WalletLate",),
    )
    # Case 2 (day 2): all four hold early.
    _case(
        session,
        chain_id=chain.id,
        source_id=source.id,
        tag="CaseTwo",
        ts=T0 + 2 * DAY,
        wallets=(*WINNERS, "WalletLate"),
    )

    # Point P3 (day 3): held early by all four wallets. WINNERS have 2
    # prior cases each; WalletLate has only 1 (case 2 — its case-1 row
    # was outside the window), so exactly 3 repeat winners count.
    late_id = _asset(session, chain.id, "LatePoint")
    _point(session, asset_id=late_id, ts=T0 + 3 * DAY, gain=0.2)
    _holders(
        session,
        asset_id=late_id,
        source_id=source.id,
        ts=T0 + 3 * DAY - H,
        wallets=(*WINNERS, "WalletLate"),
    )
    session.commit()

    evaluations = _by_asset(evaluate_points(session, as_of=T0 + 4 * DAY))
    # At P0 none of the four wallets had any prior case-win: their wins
    # all land after P0, so none of them counts there.
    assert evaluations[early_id].repeat_winner_holders == 0
    assert evaluations[early_id].converged is False
    # At P3 the three genuine repeat winners count; WalletLate does not.
    assert evaluations[late_id].repeat_winner_holders == 3
    assert evaluations[late_id].converged is True


def test_holder_row_observed_after_decision_does_not_count(session):
    """A holder row inside the ts window but observed only after the
    decision is not point-in-time evidence and must not count."""
    chain, source = seed_reference(session)
    _seed_repeat_winners(session, chain_id=chain.id, source_id=source.id)

    point_id = _asset(session, chain.id, "LateObserved")
    ts = T0 + 3 * DAY
    _point(session, asset_id=point_id, ts=ts, gain=0.2)
    _holders(
        session,
        asset_id=point_id,
        source_id=source.id,
        ts=ts - H,
        wallets=("WalletA", "WalletB"),
    )
    _holders(
        session,
        asset_id=point_id,
        source_id=source.id,
        ts=ts - H,
        wallets=("WalletW",),
        observed_at=ts + H,  # restated/arrived after the decision
    )
    session.commit()

    evaluations = _by_asset(evaluate_points(session, as_of=T0 + 4 * DAY))
    assert evaluations[point_id].repeat_winner_holders == 2
    assert evaluations[point_id].converged is False


def test_points_observed_after_as_of_are_excluded(session):
    chain, source = seed_reference(session)
    _seed_repeat_winners(session, chain_id=chain.id, source_id=source.id)
    point_id = _asset(session, chain.id, "ConvergedPump")
    ts = T0 + 3 * DAY
    _point(session, asset_id=point_id, ts=ts, gain=1.5)
    _holders(
        session,
        asset_id=point_id,
        source_id=source.id,
        ts=ts - H,
        wallets=WINNERS,
    )
    session.commit()

    early = convergence_stats(session, as_of=T0 + 2 * DAY + 12 * H)
    assert early.points == 2  # only the two seeding cases exist yet
    assert early.converged_points == 0

    later = convergence_stats(session, as_of=T0 + 4 * DAY)
    assert later.points == 3
    assert later.converged_points == 1
    assert later.converged_pumped == 1


# ---------------------------------------------------------------------------
# Matching: controls share the converged points' day + liquidity decile
# ---------------------------------------------------------------------------


def _with_liquidity(
    session, *, chain_id: int, source_id: int, asset_id: int, ts: datetime, reserve_usd: float
) -> None:
    pool, _pair = upsert_pool_and_pair(
        session,
        chain_id=chain_id,
        dex_id="raydium",
        pair_address=f"Pair{asset_id}Address",
        base_asset_id=asset_id,
        quote_asset_id=None,
        created_at_source=T0,
    )
    insert_liquidity_snapshot_once(
        session,
        pool_id=pool.id,
        source_id=source_id,
        ts=ts,
        observed_at=ts,
        reserve_usd=reserve_usd,
    )


def test_decile_matching_restricts_controls(session):
    chain, source = seed_reference(session)
    _seed_repeat_winners(session, chain_id=chain.id, source_id=source.id)
    ts = T0 + 3 * DAY

    converged_id = _asset(session, chain.id, "DeepConverged")
    _point(session, asset_id=converged_id, ts=ts, gain=0.2)
    _holders(session, asset_id=converged_id, source_id=source.id, ts=ts - H, wallets=WINNERS)
    _with_liquidity(
        session,
        chain_id=chain.id,
        source_id=source.id,
        asset_id=converged_id,
        ts=ts - H,
        reserve_usd=10_000.0,
    )
    # Two non-converged points at the same depth (same decile): the
    # only legitimate controls for the converged point.
    for tag, gain in (("DeepControlA", 1.5), ("DeepControlB", 0.2)):
        asset_id = _asset(session, chain.id, tag)
        _point(session, asset_id=asset_id, ts=ts, gain=gain)
        _holders(session, asset_id=asset_id, source_id=source.id, ts=ts - H, wallets=("WalletD",))
        _with_liquidity(
            session,
            chain_id=chain.id,
            source_id=source.id,
            asset_id=asset_id,
            ts=ts - H,
            reserve_usd=10_000.0,
        )
    # Seven shallow points, all pumped: a broken matcher that pooled
    # the whole day would let them swamp the control group.
    for i in range(7):
        asset_id = _asset(session, chain.id, f"Shallow{i}")
        _point(session, asset_id=asset_id, ts=ts, gain=1.5)
        _holders(session, asset_id=asset_id, source_id=source.id, ts=ts - H, wallets=("WalletD",))
        _with_liquidity(
            session,
            chain_id=chain.id,
            source_id=source.id,
            asset_id=asset_id,
            ts=ts - H,
            reserve_usd=10.0,
        )
    session.commit()

    stats = convergence_stats(session, as_of=T0 + 4 * DAY)
    assert stats.converged_points == 1
    assert stats.control_points == 2
    assert stats.control_pumped == 1
    assert stats.control_pump_share == 0.5


# ---------------------------------------------------------------------------
# The gate verdict (run_case_control writes the report either way)
# ---------------------------------------------------------------------------


def _seed_gate_population(
    session,
    *,
    converged_pumps: int,
    converged_total: int,
    control_pumps: int,
    control_total: int,
) -> datetime:
    """One same-ts population on day 3: converged points held early by
    the repeat winners, controls held by a wallet that never won."""
    chain, source = seed_reference(session)
    _seed_repeat_winners(session, chain_id=chain.id, source_id=source.id)
    ts = T0 + 3 * DAY + 12 * H
    for i in range(converged_total):
        asset_id = _asset(session, chain.id, f"Conv{i:02d}")
        _point(
            session,
            asset_id=asset_id,
            ts=ts,
            gain=1.5 if i < converged_pumps else 0.2,
        )
        _holders(session, asset_id=asset_id, source_id=source.id, ts=ts - H, wallets=WINNERS)
    for i in range(control_total):
        asset_id = _asset(session, chain.id, f"Ctrl{i:02d}")
        _point(
            session,
            asset_id=asset_id,
            ts=ts,
            gain=1.5 if i < control_pumps else 0.2,
        )
        _holders(session, asset_id=asset_id, source_id=source.id, ts=ts - H, wallets=("WalletD",))
    session.commit()
    return ts


def test_gate_passes_when_converged_points_pump_at_3x(session, tmp_path):
    # Converged pump share 24/30 = 0.80 vs matched-control 3/30 = 0.10:
    # ratio 8.0 >= 3.0 with n = 30 converged points -> exit 0.
    _seed_gate_population(
        session, converged_pumps=24, converged_total=30, control_pumps=3, control_total=30
    )
    exit_code = run_case_control(session=session, as_of=T0 + 4 * DAY, reports_dir=tmp_path)
    assert exit_code == 0
    payload = json.loads(
        (tmp_path / f"smart-money-case-control-{(T0 + 4 * DAY).strftime('%Y%m%d')}.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["passed"] is True
    assert payload["converged_points"] == 30
    assert payload["converged_pumped"] == 24
    assert payload["converged_pump_share"] == 0.8
    assert payload["control_points"] == 30
    assert payload["control_pumped"] == 3
    assert payload["control_pump_share"] == 0.1
    assert payload["pump_share_ratio"] == 8.0


def test_gate_fails_when_converged_share_below_3x(session, tmp_path):
    # Converged 6/30 = 0.20 vs control 3/30 = 0.10: ratio 2.0 < 3.0.
    _seed_gate_population(
        session, converged_pumps=6, converged_total=30, control_pumps=3, control_total=30
    )
    exit_code = run_case_control(session=session, as_of=T0 + 4 * DAY, reports_dir=tmp_path)
    assert exit_code == 1
    json_path = tmp_path / f"smart-money-case-control-{(T0 + 4 * DAY).strftime('%Y%m%d')}.json"
    assert json_path.exists()
    assert json_path.with_suffix(".md").exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["pump_share_ratio"] == 2.0


def test_gate_fails_on_thin_converged_sample(session, tmp_path):
    # Ratio is 10x but n(converged) = 10 < 30: the gate cannot pass on
    # a thin sample, however good it looks.
    _seed_gate_population(
        session, converged_pumps=10, converged_total=10, control_pumps=1, control_total=10
    )
    exit_code = run_case_control(session=session, as_of=T0 + 4 * DAY, reports_dir=tmp_path)
    assert exit_code == 1
    payload = json.loads(
        (tmp_path / f"smart-money-case-control-{(T0 + 4 * DAY).strftime('%Y%m%d')}.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["passed"] is False
    assert payload["converged_points"] == 10
    assert payload["pump_share_ratio"] == 10.0


def test_gate_constants_match_the_plan():
    # The gate bars are the plan's, pinned so a quiet edit cannot move
    # them: >= 3x pump-share ratio, n >= 30 converged points, cases at
    # forward_max_gain_pct >= 1.0, repeat winners at >= 2 prior cases,
    # convergence at >= 3 repeat-winner holders inside 6h.
    assert case_control_smart_money.PUMP_GAIN_THRESHOLD == 1.0
    assert case_control_smart_money.GATE_MIN_RATIO == 3.0
    assert case_control_smart_money.GATE_MIN_CONVERGED_N == 30
    assert case_control_smart_money.REPEAT_WINNER_MIN_CASES == 2
    assert case_control_smart_money.CONVERGENCE_MIN_WALLETS == 3
    assert case_control_smart_money.HOLDER_WINDOW_HOURS == 6


def test_holder_model_has_no_pnl_history():
    # 12a's premise, pinned: per-wallet trade/PnL history does not
    # exist in the lake (only holder snapshots), which is why the gate
    # runs on the repeat-winner proxy and 12b needs its own table.
    columns = {column.name for column in models.Holder.__table__.columns}
    assert columns == {
        "id",
        "asset_id",
        "wallet_address",
        "ts",
        "observed_at",
        "source_id",
        "balance",
        "pct_supply",
    }
