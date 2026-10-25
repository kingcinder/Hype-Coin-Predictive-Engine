"""Regression tests for the findings in ``docs/leakage-audit.md``.

Every test here targets one audit finding and asserts the point-in-time
invariant the original code violated: a feature or label computed from data
that wasn't known at its decision time.  Each test is written so that the
pre-fix code (the leak) fails it and the fixed code passes.

Findings covered:
- #1  HIGH  ``deployer_history_available`` not point-in-time
- #2  HIGH  ``website_presence`` / ``github_presence_public`` read live Asset
- #3  HIGH  ``collapse_probability_24h`` model-output feedback loop
- #4  MED   ``rpc_pool_health`` live in-memory state
- #5  MED   bootstrap labels without ``observed_at`` guards
- #6  LOW   dense-label entry interpolated from a future snapshot
- #7  LOW   dense-label forward windows from raw future snapshots
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from data_lake.labels import (
    _interpolate_price,
    generate_dense_labels,
    label_generation_progress,
    replay_historical_labels,
)
from features.factory import FeatureFactory, compute_market_block
from forecast.engine import _LEAKAGE_FEATURES, FORECAST_FEATURE_NAMES
from forecast.labels import LABEL_COLLAPSE, seed_labels_at_feature_timestamps
from storage import models
from storage.repository import (
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    upsert_asset,
    upsert_feature,
    upsert_pool_and_pair,
)
from tests.conftest import seed_market_asset, seed_reference
from validation.harness import audit_feature_leakage
from validation.leakage_guard import check_rows_point_in_time

T0 = datetime(2026, 5, 1, 0, 0)  # naive for SQLite compatibility
DECISION = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _seed_arc(
    session, *, symbol: str, prices: list[float], feature_hours: list[int]
) -> models.Asset:
    """Asset with hourly snapshots plus features at the given hours."""
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=f"Addr{symbol}11111111111111111111111111111111111",
        symbol=symbol,
        name=symbol,
        first_seen_at=T0,
        website_url="https://example.org",
        github_url="https://github.com/example/hype",
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
    pool.created_at = T0
    pair.created_at = T0
    for hour, price in enumerate(prices):
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
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            reserve_usd=200_000,
        )
    for hour in feature_hours:
        upsert_feature(
            session,
            asset_id=asset.id,
            decision_ts=T0 + timedelta(hours=hour),
            feature_name="liquidity_depth",
            feature_value=200_000.0,
            source_count=1,
            freshness_score=1.0,
            missing_flag=False,
        )
    return asset


# ── Finding 1: deployer_history_available must be point-in-time ─────────────


def test_deployer_history_ignores_contracts_observed_after_decision(session) -> None:
    """A contract with a deployer wallet inspected AFTER the decision must not
    count toward a historical deployer-history snapshot."""
    asset = seed_market_asset(session)
    chain = session.get(models.Chain, asset.chain_id)
    session.add(
        models.Contract(
            chain_id=chain.id,
            asset_id=asset.id,
            address="0xAfter111111111111111111111111111111111111",
            deployer_wallet="0xDeployer111111111111111111111111111111",
            observed_at=DECISION + timedelta(hours=1),
        )
    )
    session.commit()
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }
    assert values["deployer_history_available"].value == 0.0
    assert values["deployer_history_available"].missing is True


def test_deployer_history_ignores_contract_records_created_after_decision(session) -> None:
    asset = seed_market_asset(session)
    contract = models.Contract(
        chain_id=asset.chain_id,
        asset_id=asset.id,
        address="0xLateRecord1111111111111111111111111111111",
        deployer_wallet="0xDeployer111111111111111111111111111111",
        observed_at=DECISION - timedelta(hours=1),
        created_at=DECISION + timedelta(hours=1),
    )
    session.add(contract)
    session.flush()

    assert FeatureFactory()._deployer_history(session, asset.id, DECISION) is None


def test_contract_features_are_missing_without_prior_contract_evidence(session) -> None:
    asset = seed_market_asset(session)
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }

    for name in ("suspicious_contract_flags", "deployer_history_available"):
        assert values[name].missing is True
        assert values[name].source_count == 0


def test_contract_flags_reject_future_event_times_even_when_observed_early(session) -> None:
    asset = seed_market_asset(session)
    chain, source = seed_reference(session)
    contract = models.Contract(
        chain_id=chain.id,
        asset_id=asset.id,
        address="0xFlagged11111111111111111111111111111111",
        observed_at=DECISION - timedelta(hours=1),
        created_at=DECISION - timedelta(hours=1),
    )
    session.add(contract)
    session.flush()
    session.add(
        models.ContractFlag(
            contract_id=contract.id,
            source_id=source.id,
            ts=DECISION + timedelta(hours=1),
            observed_at=DECISION,
            flag_type="future_event",
            severity="critical",
        )
    )
    session.flush()

    assert FeatureFactory()._contract_flag_count(session, asset.id, DECISION) == 0.0


def test_deployer_history_counts_contracts_observed_at_decision(session) -> None:
    """A contract with a deployer wallet observed at/before the decision counts."""
    asset = seed_market_asset(session)
    chain = session.get(models.Chain, asset.chain_id)
    session.add(
        models.Contract(
            chain_id=chain.id,
            asset_id=asset.id,
            address="0xAt1111111111111111111111111111111111111111",
            deployer_wallet="0xDeployer111111111111111111111111111111",
            observed_at=DECISION,
            created_at=DECISION,
        )
    )
    session.commit()
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }
    assert values["deployer_history_available"].value == 1.0


def test_historical_feature_materialization_is_not_source_availability(session) -> None:
    """Feature.observed_at records when a backfill was materialized, not when
    its input rows became knowable. The source rows still obey the PIT cutoff."""
    asset = _seed_arc(session, symbol="MATERIALIZED", prices=[1.0] * 48, feature_hours=[11])
    decision = T0 + timedelta(hours=11)
    feature = session.scalar(
        select(models.Feature).where(
            models.Feature.asset_id == asset.id,
            models.Feature.decision_ts == decision,
            models.Feature.feature_name == "liquidity_depth",
        )
    )
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    sources = list(
        session.scalars(
            select(models.MarketSnapshot).where(
                models.MarketSnapshot.pair_id == pair.id,
                models.MarketSnapshot.observed_at <= decision,
            )
        )
    )

    assert feature is not None and feature.observed_at > decision
    assert len(sources) == 12
    assert check_rows_point_in_time(sources, decision, feature_name="market_block") == 12
    values = {v.name: v for v in FeatureFactory().build_for_asset(session, asset, decision)}
    assert values["liquidity_depth"].value == 200_000.0


def test_leakage_audit_ignores_missing_zero_placeholders(session) -> None:
    """A zero placeholder marked missing must not count as a late observation."""
    asset = seed_market_asset(session)
    outcomes = []
    for index in range(12):
        decision = DECISION + timedelta(minutes=index)
        missing = index >= 10
        collapsed = 6 <= index < 10
        upsert_feature(
            session,
            asset_id=asset.id,
            decision_ts=decision,
            feature_name="hype",
            feature_value=float(collapsed) if not missing else 0.0,
            source_count=0 if missing else 1,
            freshness_score=0.0 if missing else 1.0,
            missing_flag=missing,
        )
        feature = session.scalar(
            select(models.Feature).where(
                models.Feature.asset_id == asset.id,
                models.Feature.decision_ts == decision,
                models.Feature.feature_name == "hype",
            )
        )
        if missing:
            feature.observed_at = decision + timedelta(hours=1)
        else:
            feature.observed_at = decision
        outcomes.append(
            SimpleNamespace(
                asset_id=asset.id,
                decision_ts=decision,
                collapsed=collapsed,
                rugged=False,
            )
        )
    session.flush()

    report, n = audit_feature_leakage(session, outcomes)

    assert n == 12
    assert not report.flagged


def test_market_block_rejects_future_event_timestamps_and_late_pairs() -> None:
    decision = DECISION
    prior_pair = SimpleNamespace(
        created_at_source=decision - timedelta(hours=2),
        created_at=decision - timedelta(hours=2),
    )
    late_pair = SimpleNamespace(
        created_at_source=decision - timedelta(days=1),
        created_at=decision + timedelta(hours=1),
    )
    asset = SimpleNamespace(first_seen_at=decision - timedelta(days=2))
    market_rows = [
        SimpleNamespace(
            pair_id=1,
            ts=decision - timedelta(minutes=1),
            observed_at=decision - timedelta(minutes=1),
            price_usd=1.0,
            volume_usd=10.0,
            buys=1,
            sells=1,
            source_id=1,
        ),
        SimpleNamespace(
            pair_id=2,
            ts=decision + timedelta(seconds=1),
            observed_at=decision,
            price_usd=10.0,
            volume_usd=100.0,
            buys=10,
            sells=1,
            source_id=2,
        ),
    ]
    liquidity_rows = [
        SimpleNamespace(
            pool_id=1,
            ts=decision - timedelta(minutes=1),
            observed_at=decision - timedelta(minutes=1),
            reserve_usd=100.0,
            source_id=1,
        ),
        SimpleNamespace(
            pool_id=2,
            ts=decision + timedelta(seconds=1),
            observed_at=decision,
            reserve_usd=999.0,
            source_id=2,
        ),
    ]

    values = compute_market_block(
        asset=asset,
        pairs=[prior_pair, late_pair],
        market_rows=market_rows,
        liquidity_rows=liquidity_rows,
        decision_ts=decision,
    )

    assert values["pair_age_minutes"].value == 120.0
    assert values["volume_acceleration"].missing is True
    assert values["liquidity_depth"].value == 100.0


def test_market_factory_ignores_liquidity_for_a_late_created_pool(session) -> None:
    asset = seed_market_asset(session)
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    pool = session.get(models.Pool, pair.pool_id)
    pool.created_at = DECISION + timedelta(hours=1)
    session.flush()

    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }

    assert values["liquidity_depth"].missing is True


# ── Finding 2: website/github presence must be unknown, not leaked ──────────


def test_website_presence_is_unknown_without_prior_evidence(session) -> None:
    """A URL on the live Asset row with no evidence observed at/before the
    decision reads as UNKNOWN (missing), never a confident 0.0 (silent zero)
    nor a 1.0 (leaked live value)."""
    asset = seed_market_asset(session)  # has website_url + github_url set
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }
    assert values["website_presence"].value == 0.0
    assert values["website_presence"].missing is True
    assert values["website_presence"].source_count == 0
    assert values["github_presence_public"].value == 0.0
    assert values["github_presence_public"].missing is True


def test_website_presence_confident_when_evidenced_before_decision(session) -> None:
    """Evidence observed at/before the decision flips presence to a confident 1.0."""
    asset = seed_market_asset(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    session.add(
        models.SocialMention(
            asset_id=asset.id,
            topic=asset.symbol,
            source_id=source.id,
            ts=DECISION,
            observed_at=DECISION,
            raw_ref="https://example.org/announcement",
            metrics_json={},
        )
    )
    session.commit()
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }
    assert values["website_presence"].value == 1.0
    assert values["website_presence"].missing is False
    assert values["website_presence"].source_count == 1


def test_website_presence_unknown_for_future_evidence_only(session) -> None:
    """Adding evidence AFTER the decision must not flip the historical value."""
    asset = seed_market_asset(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    session.add(
        models.SocialMention(
            asset_id=asset.id,
            topic=asset.symbol,
            source_id=source.id,
            ts=DECISION + timedelta(hours=2),
            observed_at=DECISION + timedelta(hours=2),
            raw_ref="https://example.org/announcement",
            metrics_json={},
        )
    )
    session.commit()
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }
    assert values["website_presence"].missing is True
    assert values["website_presence"].value == 0.0


# ── Finding 3: forecast must not train on its own output ────────────────────


def test_collapse_probability_24h_excluded_from_forecast_features() -> None:
    """The model's own prior output must never enter the training matrix
    (feedback loop / target leak)."""
    assert "collapse_probability_24h" in _LEAKAGE_FEATURES
    assert "collapse_probability_24h" not in FORECAST_FEATURE_NAMES


# ── Finding 4: rpc_pool_health must be persisted, not live memory ───────────


def test_rpc_pool_health_ignores_future_snapshots(session) -> None:
    """A snapshot recorded AFTER the decision must not leak into the feature."""
    asset = seed_market_asset(session)
    chain = session.get(models.Chain, asset.chain_id)
    session.add(
        models.RpcPoolSnapshot(
            chain_slug=chain.slug,
            url="https://rpc-future.example.com",
            ts=DECISION + timedelta(hours=2),
            health=0.0,
            consecutive_failures=3,
            down=True,
            probe_count=5,
            probe_successes=0,
            probe_failures=5,
        )
    )
    session.commit()
    values = {
        value.name: value for value in FeatureFactory().build_for_asset(session, asset, DECISION)
    }
    # No snapshot observed at/before the decision -> neutral healthy baseline,
    # not the future 0.0.
    assert values["rpc_pool_health"].value == 1.0


# ── Finding 5: bootstrap labels must not use unobserved price data ──────────


def test_bootstrap_labels_ignore_late_observed_forward_snapshots(session) -> None:
    """A crash snapshot whose ts lies inside the forward window but whose
    observed_at is AFTER the generation time must not flip the label."""
    asset = _seed_arc(session, symbol="LATE", prices=[1.0] * 24, feature_hours=[11])
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    # Crash at hour 13, but only observed an hour AFTER the generation time.
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=T0 + timedelta(hours=13),
        observed_at=T0 + timedelta(hours=49),  # T0+48 is the generation time
        price_usd=0.2,
        volume_usd=1_000,
        buys=10,
        sells=5,
    )
    session.commit()
    seed_labels_at_feature_timestamps(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    label = session.scalar(
        select(models.Label).where(
            models.Label.asset_id == asset.id,
            models.Label.label_type == LABEL_COLLAPSE,
        )
    )
    assert label is not None
    # Without the observed_at guard the crash (-80%) would force collapse=1.
    assert label.label_value == "0"


def test_bootstrap_labels_ignore_late_observed_entry_snapshots(session) -> None:
    """An entry snapshot ingested after the generation time must not be used
    as the label's entry price.

    The late row lands at a *distinct* ts (10:30) so ``insert_market_snapshot_once``
    first-wins dedup (pair, ts, source) cannot silently no-op it: without the
    ``observed_at`` guard it becomes the latest entry ≤ hour 11 and forces a
    phantom collapse; with the guard it is excluded and the hour-10 $1 entry
    stays."""
    asset = _seed_arc(session, symbol="LENT", prices=[1.0] * 24, feature_hours=[11])
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    # A $5 entry observed only after the generation time would make the label
    # read as a collapse (1/5 = -80%) even though flat $1 prices were real.
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=T0 + timedelta(hours=10, minutes=30),
        observed_at=T0 + timedelta(hours=49),
        price_usd=5.0,
        volume_usd=1_000,
        buys=10,
        sells=5,
    )
    session.commit()
    seed_labels_at_feature_timestamps(session, decision_ts=T0 + timedelta(hours=48))
    session.commit()
    label = session.scalar(
        select(models.Label).where(
            models.Label.asset_id == asset.id,
            models.Label.label_type == LABEL_COLLAPSE,
        )
    )
    assert label is not None
    assert label.label_value == "0"


# ── Finding 6: dense-label entry must be backward-only ──────────────────────


def test_dense_entry_interpolation_is_backward_only() -> None:
    """The entry price at an hourly grid point is the last observed price at or
    before it — never interpolated toward the future-adjacent snapshot."""
    aware0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    snap0 = SimpleNamespace(ts=aware0, price_usd=1.0)
    snap1 = SimpleNamespace(ts=aware0 + timedelta(hours=2), price_usd=2.0)
    midpoint = aware0 + timedelta(hours=1)
    # Old behavior interpolated toward the future snapshot (~1.41, a 41%
    # phantom magnitude).  Point-in-time entry is the last known price: 1.0.
    assert _interpolate_price([snap0, snap1], midpoint) == 1.0
    # Target exactly on a snapshot uses that snapshot.
    assert _interpolate_price([snap0, snap1], aware0 + timedelta(hours=2)) == 2.0
    # Target before any snapshot -> None (no fabricated entry).
    assert _interpolate_price([snap0, snap1], aware0 - timedelta(hours=1)) is None
    # Empty series -> None.
    assert _interpolate_price([], midpoint) is None


def test_dense_entry_rejects_prices_observed_after_the_decision() -> None:
    decision = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    timely = SimpleNamespace(
        ts=decision - timedelta(hours=2), observed_at=decision - timedelta(hours=2), price_usd=1.0
    )
    late = SimpleNamespace(
        ts=decision - timedelta(hours=1), observed_at=decision + timedelta(hours=1), price_usd=5.0
    )

    assert _interpolate_price([timely, late], decision) == 1.0


def test_historical_label_replay_uses_point_in_time_entry_and_bounded_targets(session) -> None:
    asset = _seed_arc(session, symbol="REPLAY", prices=[1.0] * 48, feature_hours=[11])
    feature = session.scalar(
        select(models.Feature).where(
            models.Feature.asset_id == asset.id,
            models.Feature.decision_ts == T0 + timedelta(hours=11),
        )
    )
    assert feature is not None
    feature.observed_at = T0 + timedelta(hours=11)
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    as_of = T0 + timedelta(hours=48)

    # The late entry would manufacture a collapse if replay used market time only.
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=T0 + timedelta(hours=10, minutes=30),
        observed_at=as_of + timedelta(hours=1),
        price_usd=5.0,
        volume_usd=1_000,
    )
    # Neither a future beyond the 24h target window nor a late observed pump may count.
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=T0 + timedelta(hours=36),
        observed_at=T0 + timedelta(hours=36),
        price_usd=0.2,
        volume_usd=1_000,
    )
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=T0 + timedelta(hours=20),
        observed_at=as_of + timedelta(hours=1),
        price_usd=4.0,
        volume_usd=1_000,
    )

    counts = replay_historical_labels(session, decision_ts=as_of)

    ignition = session.scalar(
        select(models.Label).where(
            models.Label.asset_id == asset.id,
            models.Label.ts == T0 + timedelta(hours=11),
            models.Label.label_type == "ignition",
        )
    )
    collapse = session.scalar(
        select(models.Label).where(
            models.Label.asset_id == asset.id,
            models.Label.ts == T0 + timedelta(hours=11),
            models.Label.label_type == "collapse",
        )
    )
    assert counts["decision_points"] == 1
    assert counts["labels_created"] == 2
    assert ignition is not None and ignition.label_value == "0"
    assert collapse is not None and collapse.label_value == "0"
    assert ignition.label_source.startswith("historical-replay:")
    assert ignition.observed_at <= as_of


def test_forecast_status_uses_trainable_feature_aligned_label_pairs(session) -> None:
    asset = _seed_arc(session, symbol="PROGRESS", prices=[1.0] * 48, feature_hours=[11])
    feature_ts = T0 + timedelta(hours=11)
    feature = session.scalar(
        select(models.Feature).where(
            models.Feature.asset_id == asset.id,
            models.Feature.decision_ts == feature_ts,
        )
    )
    assert feature is not None
    session.add_all(
        [
            models.Label(
                asset_id=asset.id,
                ts=feature_ts,
                observed_at=DECISION,
                label_type="ignition",
                label_value="0",
                label_source="fixture",
            ),
            models.Label(
                asset_id=asset.id,
                ts=feature_ts,
                observed_at=DECISION,
                label_type="collapse",
                label_value="0",
                label_source="fixture",
            ),
        ]
    )
    session.flush()

    progress = label_generation_progress(session)

    assert progress["usable_forecast_samples"] == 1
    assert progress["forecast_status"] == "Forecasting not live: 1/30 labels"
    assert progress["forecast_validated"] is False
    assert "Unvalidated" in progress["forecast_validation_note"]
    assert "1/30" in progress["forecast_validation_note"]


# ── Finding 7: dense-label forward windows must respect observed_at ─────────


def test_dense_labels_ignore_late_observed_forward_prices(session) -> None:
    """A pump snapshot observed after the generation time must not appear in a
    dense label's forward window."""
    asset = _seed_arc(session, symbol="DLATE", prices=[1.0] * 30, feature_hours=[])
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    # A 3x pump at hour 20, but only observed after the generation time.
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=T0 + timedelta(hours=20),
        observed_at=T0 + timedelta(hours=73),  # generation time is T0+72
        price_usd=3.0,
        volume_usd=1_000,
        buys=10,
        sells=5,
    )
    session.commit()
    counts = generate_dense_labels(
        session,
        decision_ts=T0 + timedelta(hours=72),
        forward_hours=24,
        ignition_threshold=0.5,
        collapse_threshold=-0.5,
    )
    session.commit()
    # Without the observed_at gate the 3x pump would trigger ignition=1.
    assert counts["ignition"] == 0
