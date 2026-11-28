"""Tests for the features-table retention policy (ops.features_retention).

The policy is label-anchored: a feature row is kept only if its
(asset_id, decision_ts) key is labeled, in the pending/resolving window
for a label that has not landed yet, or at one of the latest two
decision_ts per asset. Everything else is pruned.

The fixture seeds three assets on a fixed timeline (T0 = newest
decision_ts) that exercises every rule and every prune case:

- Asset A (pair with entry + future snapshots): an old labeled key, an old
  unlabeled key, a resolving-band key with a future price, a resolving-band
  key without one, and pending keys with entry prices.
- Asset B (pair with a single late snapshot): old/resolving/pending keys
  with no entry price, and pending keys with one.
- Asset C (no pair, so no snapshots at all): only the latest-two rule can
  keep its rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from common.config import Settings
from ops.features_retention import (
    _prunable_clause,  # noqa: PLC2701 - white-box test of the mid-pass guard
    build_keep_keys,
    features_retention_due,
    run_features_retention,
)
from storage import models
from storage.database import Base
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_market_snapshot_once,
    upsert_asset,
    upsert_pool_and_pair,
)

T0 = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)
H = timedelta(hours=1)
FEATURE_NAMES = ("liquidity_depth", "buy_sell_ratio", "unique_buyers_estimate")


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/features-retention-test.db")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None,
        features_retention_enabled=True,
        features_retention_cadence_hours=24.0,
        features_retention_pending_hours=24.0,
        features_retention_resolving_hours=72.0,
        features_retention_latest_per_asset=2,
        features_retention_max_rows_per_pass=0,
        features_retention_batch_size=1000,
        forecast_forward_hours=24,
    )


def _seed(engine):
    """Seed the three-asset timeline; return the expected keep/prune keys."""
    with Session(engine) as session:
        chain = get_or_create_chain(
            session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
        )
        source = get_or_create_source(
            session,
            name="dexscreener",
            source_type="market_data",
            tier="venue",
            base_url="https://api.dexscreener.com",
        )
        quote = upsert_asset(
            session,
            chain_id=chain.id,
            address="So11111111111111111111111111111111111111112",
            symbol="WSOL",
            name="Wrapped SOL",
            first_seen_at=T0 - 200 * H,
        )
        assets: dict[str, int] = {}
        pairs: dict[str, int] = {}
        for tag in ("A", "B", "C"):
            asset = upsert_asset(
                session,
                chain_id=chain.id,
                address=f"Asset{tag}1111111111111111111111111111111111",
                symbol=f"T{tag}",
                name=f"Token {tag}",
                first_seen_at=T0 - 200 * H,
            )
            assets[tag] = asset.id
        for tag in ("A", "B"):
            _, pair = upsert_pool_and_pair(
                session,
                chain_id=chain.id,
                dex_id="raydium",
                pair_address=f"Pair{tag}1111111111111111111111111111111111",
                base_asset_id=assets[tag],
                quote_asset_id=quote.id,
                created_at_source=T0 - 200 * H,
            )
            pairs[tag] = pair.id
        # Asset A: entry + future coverage across the timeline.
        for ts, price in (
            (T0 - 100 * H, 0.001),
            (T0 - 30 * H, 0.002),
            (T0 - 20 * H, 0.003),
            (T0 - 10 * H, 0.004),
            (T0 - 1 * H, 0.005),
        ):
            insert_market_snapshot_once(
                session,
                pair_id=pairs["A"],
                source_id=source.id,
                ts=ts,
                observed_at=ts,
                price_usd=price,
                volume_usd=1000.0,
                buys=1,
                sells=0,
            )
        # Asset B: a single late snapshot -- nothing before T0-5h has an entry.
        insert_market_snapshot_once(
            session,
            pair_id=pairs["B"],
            source_id=source.id,
            ts=T0 - 5 * H,
            observed_at=T0 - 5 * H,
            price_usd=0.01,
            volume_usd=1000.0,
            buys=1,
            sells=0,
        )

        def features(asset_id: int, ts: datetime) -> None:
            for i, name in enumerate(FEATURE_NAMES):
                session.add(
                    models.Feature(
                        asset_id=asset_id,
                        decision_ts=ts,
                        observed_at=ts,
                        feature_name=name,
                        feature_value=float(i + 1),
                        source_count=1,
                        freshness_score=1.0,
                        missing_flag=False,
                        source_refs={},
                    )
                )

        def labels(asset_id: int, ts: datetime) -> None:
            for label_type in ("ignition", "collapse"):
                session.add(
                    models.Label(
                        asset_id=asset_id,
                        ts=ts,
                        observed_at=ts,
                        label_type=label_type,
                        label_value="1",
                        label_source="test",
                    )
                )

        # Asset A.
        features(assets["A"], T0 - 100 * H)
        labels(assets["A"], T0 - 100 * H)  # labeled -> keep (rule 1)
        features(assets["A"], T0 - 90 * H)  # old, outside resolving -> prune
        features(assets["A"], T0 - 60 * H)  # resolving, entry but no future -> prune
        features(assets["A"], T0 - 30 * H)  # resolving, entry + future -> keep (rule 3)
        features(assets["A"], T0 - 10 * H)  # pending + entry -> keep (rule 2)
        features(assets["A"], T0 - 1 * H)  # pending + entry, latest -> keep
        features(assets["A"], T0)  # newest -> keep
        # Asset B.
        features(assets["B"], T0 - 100 * H)  # old, no entry -> prune
        features(assets["B"], T0 - 50 * H)  # resolving, no entry -> prune
        features(assets["B"], T0 - 10 * H)  # pending, no entry -> prune
        features(assets["B"], T0 - 5 * H)  # pending + entry -> keep
        features(assets["B"], T0 - 2 * H)  # pending + entry, latest -> keep
        # Asset C (no pair, no snapshots: only the latest-two rule can keep it).
        features(assets["C"], T0 - 100 * H)  # old -> prune
        features(assets["C"], T0 - 10 * H)  # latest-2 -> keep (rule 4)
        features(assets["C"], T0 - 1 * H)  # latest-2 -> keep (rule 4)
        session.commit()
    a, b, c = assets["A"], assets["B"], assets["C"]
    keep = {
        (a, T0 - 100 * H),
        (a, T0 - 30 * H),
        (a, T0 - 10 * H),
        (a, T0 - 1 * H),
        (a, T0),
        (b, T0 - 5 * H),
        (b, T0 - 2 * H),
        (c, T0 - 10 * H),
        (c, T0 - 1 * H),
    }
    prune = {
        (a, T0 - 90 * H),
        (a, T0 - 60 * H),
        (b, T0 - 100 * H),
        (b, T0 - 50 * H),
        (b, T0 - 10 * H),
        (c, T0 - 100 * H),
    }
    return {"keep": keep, "prune": prune, "assets": assets}


def _as_utc(ts: datetime) -> datetime:
    # SQLite does not preserve tzinfo on round-trip; the stored values are UTC.
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _keys(engine):
    with Session(engine) as session:
        return {
            (row.asset_id, _as_utc(row.decision_ts)) for row in session.query(models.Feature).all()
        }


def _count(engine) -> int:
    with Session(engine) as session:
        return session.query(models.Feature).count()


def test_prune_keeps_needed_rows_and_removes_only_prunable_ones(engine, settings):
    expected = _seed(engine)
    assert _count(engine) == 45

    result = run_features_retention(engine, settings=settings)

    assert result.get("error") is None
    assert result["rows_before"] == 45
    assert result["rows_pruned"] == 18
    assert result["rows_after"] == 27
    assert result["keys_kept"] == 9
    assert result["dry_run"] is False
    remaining = _keys(engine)
    assert remaining == expected["keep"]
    assert not (expected["prune"] & remaining)


def test_prune_keeps_labeled_keys_of_any_age(engine, settings):
    expected = _seed(engine)
    run_features_retention(engine, settings=settings)
    remaining = _keys(engine)
    # The 100h-old labeled key survives; the equally old unlabeled keys do not.
    assert (expected["assets"]["A"], T0 - 100 * H) in remaining
    assert (expected["assets"]["A"], T0 - 90 * H) not in remaining
    assert (expected["assets"]["B"], T0 - 100 * H) not in remaining
    assert (expected["assets"]["C"], T0 - 100 * H) not in remaining


def test_prune_keeps_pending_with_entry_and_prunes_pending_without(engine, settings):
    expected = _seed(engine)
    run_features_retention(engine, settings=settings)
    remaining = _keys(engine)
    assert (expected["assets"]["A"], T0 - 10 * H) in remaining
    assert (expected["assets"]["B"], T0 - 10 * H) not in remaining


def test_prune_keeps_resolving_with_future_and_prunes_without(engine, settings):
    expected = _seed(engine)
    run_features_retention(engine, settings=settings)
    remaining = _keys(engine)
    assert (expected["assets"]["A"], T0 - 30 * H) in remaining
    assert (expected["assets"]["A"], T0 - 60 * H) not in remaining


def test_prune_keeps_latest_two_per_asset_even_without_prices(engine, settings):
    expected = _seed(engine)
    run_features_retention(engine, settings=settings)
    remaining = _keys(engine)
    # Asset C has no pair and no snapshots: only the latest-two rule keeps it.
    assert (expected["assets"]["C"], T0 - 10 * H) in remaining
    assert (expected["assets"]["C"], T0 - 1 * H) in remaining


def test_prune_is_idempotent(engine, settings):
    _seed(engine)
    first = run_features_retention(engine, settings=settings)
    assert first["rows_pruned"] == 18
    second = run_features_retention(engine, settings=settings)
    assert second["rows_pruned"] == 0
    assert second["rows_before"] == second["rows_after"] == 27


def test_dry_run_deletes_nothing(engine, settings):
    _seed(engine)
    result = run_features_retention(engine, dry_run=True, settings=settings)
    assert result["dry_run"] is True
    assert result["rows_pruned"] == 18  # would-be count
    assert result["rows_before"] == result["rows_after"] == 45
    assert _count(engine) == 45


def test_max_rows_cap_is_exact(engine, settings):
    _seed(engine)
    result = run_features_retention(engine, settings=settings, max_rows=5)
    assert result["rows_pruned"] == 5
    assert result["rows_after"] == 40
    # A second capped pass continues where the first stopped.
    second = run_features_retention(engine, settings=settings, max_rows=5)
    assert second["rows_pruned"] == 5
    assert second["rows_after"] == 35


def test_rows_written_after_keep_set_built_are_never_pruned(engine, settings):
    expected = _seed(engine)
    asset_c = expected["assets"]["C"]
    with engine.connect() as conn:
        as_of = conn.execute(text("SELECT MAX(decision_ts) FROM features")).scalar()
        build_keep_keys(conn, settings=settings, as_of=str(as_of))
        max_id = conn.execute(text("SELECT MAX(id) FROM features")).scalar_one()
        # Reuse the stored string form of C's oldest (prunable) decision_ts so
        # the new row lands on a key the keep-set already rejected.
        stored_ts = conn.execute(
            text(
                "SELECT decision_ts FROM features WHERE asset_id = :aid "
                "ORDER BY decision_ts LIMIT 1"
            ),
            {"aid": asset_c},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO features (asset_id, decision_ts, observed_at, "
                "feature_name, feature_value, source_count, freshness_score, "
                "missing_flag, source_refs) VALUES (:aid, :ts, :ts, "
                "'probe_feature', 1.0, 0, 0.0, 0, '{}')"
            ),
            {"aid": asset_c, "ts": stored_ts},
        )
        conn.commit()
        clause = _prunable_clause(max_id=int(max_id))
        deleted = conn.execute(text(f"DELETE FROM features WHERE {clause}")).rowcount
        conn.commit()
    # The 18 old prunable rows are gone; the mid-pass row survives.
    assert int(deleted or 0) == 18
    with Session(engine) as session:
        probe = session.query(models.Feature).filter_by(feature_name="probe_feature").one_or_none()
        assert probe is not None
        assert probe.asset_id == asset_c


def test_run_records_row_and_health(engine, settings):
    _seed(engine)
    run_features_retention(engine, settings=settings)
    with Session(engine) as session:
        runs = session.query(models.FeatureRetentionRun).all()
        assert len(runs) == 1
        assert runs[0].rows_before == 45
        assert runs[0].rows_pruned == 18
        assert runs[0].keys_kept == 9
        assert runs[0].dry_run is False
        assert runs[0].error is None
        health = session.query(models.SystemHealth).filter_by(component="features_retention").all()
        assert len(health) == 1
        assert health[0].state == "ok"
        assert "pruned=18" in (health[0].message or "")


def test_features_retention_due(engine, settings):
    _seed(engine)
    with Session(engine) as session:
        assert features_retention_due(session, settings=settings) is True
    run_features_retention(engine, settings=settings)
    with Session(engine) as session:
        assert features_retention_due(session, settings=settings) is False
        run = session.query(models.FeatureRetentionRun).one()
        # 25h later the cadence (24h) has elapsed: due again.
        assert (
            features_retention_due(
                session,
                now=run.ts + timedelta(hours=25),
                settings=settings,
            )
            is True
        )
    disabled = Settings(_env_file=None, features_retention_enabled=False)
    with Session(engine) as session:
        assert features_retention_due(session, settings=disabled) is False


def test_empty_features_table_is_a_clean_noop(engine, settings):
    result = run_features_retention(engine, settings=settings)
    assert result.get("error") is None
    assert result["rows_before"] == 0
    assert result["rows_pruned"] == 0
    assert result["keys_kept"] == 0
