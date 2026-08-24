from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from common.config import Settings
from common.time import ensure_utc, utc_now
from ops import parity as parity_module
from ops.archive import LocalArchiveStore, RawEvidenceCompactor
from ops.notifier import notify_parity_mismatch
from ops.parity import (
    parity_decision_ts,
    parity_due,
    run_parity,
)
from storage import models
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_holder_once,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    store_raw_evidence,
    upsert_asset,
    upsert_contract,
    upsert_pool_and_pair,
)

DECISION = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)
BASE_ADDRESS = "TokenParity11111111111111111111111111111111"
PAIR_ADDRESS = "PairParity1111111111111111111111111111111111"


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        archive_enabled=True,
        archive_backend="local",
        archive_local_dir=str(tmp_path),
        archive_compact_after_hours=0.0,
        archive_retention_days=30,
        archive_batch_size=5_000,
    )
    base.update(overrides)
    return Settings(**base)


def _pool_payload(
    pair_address: str,
    price: float,
    volume: float,
    liquidity: float,
    buys: int,
    sells: int,
    created_at: datetime,
) -> dict:
    """A GeckoTerminal ``new_pools`` item shaped like the live API payload."""
    return {
        "id": f"solana_{pair_address}",
        "type": "pool",
        "attributes": {
            "address": pair_address,
            "name": "PARITY / WETH",
            "pool_created_at": created_at.isoformat().replace("+00:00", "Z"),
            "base_token_price_usd": str(price),
            "volume_usd": {
                "h1": str(volume),
                "h24": str(volume * 6),
                "m5": str(round(volume / 2, 2)),
            },
            "reserve_in_usd": str(liquidity),
            "transactions": {
                "h1": {"buys": buys, "sells": sells},
                "h24": {"buys": buys * 6, "sells": sells * 6},
                "m5": {"buys": max(1, buys // 2), "sells": max(1, sells // 2)},
            },
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_{BASE_ADDRESS}", "type": "token"}},
            "quote_token": {
                "data": {
                    "id": "solana_So11111111111111111111111111111111111111112",
                    "type": "token",
                }
            },
            "dex": {"data": {"id": "solana_raydium", "type": "dex"}},
        },
    }


def _seed_consistent(session, tmp_path, settings) -> models.Asset:
    """Seed identical observations into the SQL tables AND the lake, mirroring
    the production flow: SQL normalized rows + the evidence payloads that
    produced them, compacted into partitioned Parquet."""
    chain = get_or_create_chain(
        session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
    )
    gt = get_or_create_source(
        session,
        name="geckoterminal",
        source_type="market_data",
        tier="venue",
        base_url="https://api.geckoterminal.com",
    )
    rpc = get_or_create_source(
        session, name="solana_rpc", source_type="chain_rpc", tier="chain", base_url=None
    )
    created = DECISION - timedelta(hours=6)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=BASE_ADDRESS,
        symbol="PARITY",
        name="Parity Token",
        first_seen_at=created,
    )
    quote = upsert_asset(
        session,
        chain_id=chain.id,
        address="So11111111111111111111111111111111111111112",
        symbol="WETH",
        name="Wrapped Ether",
        first_seen_at=created - timedelta(days=365),
    )
    _, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=PAIR_ADDRESS,
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=created,
    )

    # Price arc over the trailing 2h.
    arc = [(2, 1.10, 7_000, 18, 7), (1, 1.25, 18_000, 38, 14)]
    for hours_ago, price, volume, buys, sells in arc:
        observed = DECISION - timedelta(hours=hours_ago)
        raw = store_raw_evidence(
            session,
            source=gt,
            payload={
                "chain": "solana",
                "new_pools": [
                    _pool_payload(PAIR_ADDRESS, price, volume, 50_000, buys, sells, created)
                ],
            },
            observed_at=observed,
        )
        ts = observed.replace(minute=0, second=0, microsecond=0)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=gt.id,
            ts=ts,
            observed_at=observed,
            price_usd=price,
            volume_usd=volume,
            buys=buys,
            sells=sells,
            raw_evidence_id=raw.id,
        )
        insert_liquidity_snapshot_once(
            session,
            pool_id=pair.pool_id,
            source_id=gt.id,
            ts=ts,
            observed_at=observed,
            reserve_usd=50_000,
            raw_evidence_id=raw.id,
        )

    # One low-liquidity scan: reserve below the discovery threshold, which in
    # production creates an evidence-backed low_liquidity ContractFlag.
    low_observed = DECISION - timedelta(hours=3, minutes=30)
    raw_low = store_raw_evidence(
        session,
        source=gt,
        payload={
            "chain": "solana",
            "new_pools": [
                _pool_payload(PAIR_ADDRESS, 0.5, 100, 900, 1, 1, created)
            ],
        },
        observed_at=low_observed,
    )
    low_ts = low_observed.replace(minute=0, second=0, microsecond=0)
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=gt.id,
        ts=low_ts,
        observed_at=low_observed,
        price_usd=0.5,
        volume_usd=100,
        buys=1,
        sells=1,
        raw_evidence_id=raw_low.id,
    )
    insert_liquidity_snapshot_once(
        session,
        pool_id=pair.pool_id,
        source_id=gt.id,
        ts=low_ts,
        observed_at=low_observed,
        reserve_usd=900,
        raw_evidence_id=raw_low.id,
    )
    contract = upsert_contract(
        session,
        chain_id=chain.id,
        asset_id=asset.id,
        address=BASE_ADDRESS,
        observed_at=low_observed,
    )
    session.add(
        models.ContractFlag(
            contract_id=contract.id,
            source_id=gt.id,
            ts=low_observed,
            observed_at=low_observed,
            flag_type="low_liquidity",
            severity="warning",
            evidence_id=raw_low.id,
            details={"liquidity_usd": 900.0},
        )
    )

    # One contract-analysis pass: findings (mint authority + ownership not
    # renounced) become evidence-backed ContractFlag rows in production; the
    # lake must reconstruct the same count from the contract_analysis evidence.
    ca = get_or_create_source(
        session,
        name="contract_analysis",
        source_type="chain_rpc",
        tier="chain",
        base_url=None,
    )
    ca_observed = DECISION - timedelta(hours=4)
    ca_findings = [
        {"flag_type": "mint_authority", "severity": "warning"},
        {"flag_type": "ownership_not_renounced", "severity": "warning"},
    ]
    raw_ca = store_raw_evidence(
        session,
        source=ca,
        payload={
            "contract_analysis": {
                "chain": "solana",
                "asset_address": BASE_ADDRESS,
                "suspicious_flags": 2,
                "is_honeypot": False,
                "has_mint": True,
                "has_pause": False,
                "ownership_renounced": False,
                "deployer_known_rug": False,
                "reasons": [
                    "Contract has mint function (supply can be inflated)",
                    "Contract ownership not renounced",
                ],
                "findings": ca_findings,
            }
        },
        observed_at=ca_observed,
    )
    for finding in ca_findings:
        session.add(
            models.ContractFlag(
                contract_id=contract.id,
                source_id=ca.id,
                ts=ca_observed,
                observed_at=ca_observed,
                flag_type=finding["flag_type"],
                severity=finding["severity"],
                evidence_id=raw_ca.id,
                details=dict(finding),
            )
        )

    # Holder snapshots: 5 accounts two hours before the decision, 7 at the
    # decision hour (the SQL Holder rows + the RPC evidence both paths read).
    supply = 10_000.0
    snap_a = [
        ("wallet-a", 100.0),
        ("wallet-b", 200.0),
        ("wallet-c", 300.0),
        ("wallet-d", 400.0),
        ("wallet-e", 500.0),
    ]
    snap_b = snap_a + [("wallet-f", 600.0), ("wallet-g", 700.0)]
    for observed, accounts in [
        (DECISION - timedelta(hours=2), snap_a),
        (DECISION, snap_b),
    ]:
        store_raw_evidence(
            session,
            source=rpc,
            payload={
                "asset_id": asset.id,
                "mint": BASE_ADDRESS,
                "supply": supply,
                "largest_accounts": [
                    {"address": address, "uiAmountString": str(balance)}
                    for address, balance in accounts
                ],
            },
            observed_at=observed,
        )
        ts = observed.replace(minute=0, second=0, microsecond=0)
        for address, balance in accounts:
            insert_holder_once(
                session,
                asset_id=asset.id,
                wallet_address=address,
                source_id=rpc.id,
                ts=ts,
                observed_at=observed,
                balance=balance,
                pct_supply=balance / supply,
            )

    session.flush()
    # Compact half an hour after the decision so the holder snapshot observed
    # exactly at the decision time is archived too.
    RawEvidenceCompactor(
        store=LocalArchiveStore(tmp_path), settings=settings
    ).compact(session, DECISION + timedelta(minutes=30))
    session.flush()
    return asset


def _add_sql_only_holder(session, asset) -> None:
    """Introduce a SQL-only divergence: an extra holder at the decision hour
    that has no archived RPC evidence behind it."""
    rpc = session.scalar(select(models.Source).where(models.Source.name == "solana_rpc"))
    session.add(
        models.Holder(
            asset_id=asset.id,
            wallet_address="wallet-h",
            source_id=rpc.id,
            ts=DECISION,
            observed_at=DECISION,
            balance=800.0,
            pct_supply=0.08,
        )
    )
    session.flush()


def test_parity_ok_on_consistent_lake(session, tmp_path) -> None:
    """Both read paths agree on the same observations -> zero mismatches,
    ok health, no push."""
    settings = _settings(tmp_path)
    _seed_consistent(session, tmp_path, settings)
    session.commit()

    result = run_parity(session, decision_ts=DECISION, settings=settings)
    assert result["status"] == "ok"
    assert result["mismatches"] == 0
    # The seeded base asset plus the quote asset both compare clean.
    assert result["compared_assets"] == 2
    assert result["pushed"] is False  # ntfy disabled by default

    row = session.scalar(
        select(models.SystemHealth).where(
            models.SystemHealth.component == "parity"
        )
    )
    assert row is not None
    assert row.state == "ok"
    assert "0 mismatches" in row.message


def test_parity_pages_mismatch_via_ntfy(session, tmp_path, monkeypatch) -> None:
    """A lake-vs-SQL divergence trips red health and pages via ntfy with the
    first mismatches as actionable examples."""
    settings = _settings(tmp_path)
    asset = _seed_consistent(session, tmp_path, settings)
    _add_sql_only_holder(session, asset)
    session.commit()

    calls: dict = {}

    def fake_notify(
        mismatch_count, compared_assets, decision_ts, examples, *, settings=None
    ):
        calls.update(
            {"count": mismatch_count, "assets": compared_assets, "examples": examples}
        )
        return True

    monkeypatch.setattr(parity_module, "notify_parity_mismatch", fake_notify)

    result = run_parity(session, decision_ts=DECISION, settings=settings)
    assert result["status"] == "red"
    assert result["mismatches"] >= 1
    assert result["pushed"] is True
    assert calls["count"] == result["mismatches"]
    assert calls["assets"] == 2
    assert any(
        "holder_count" in example or "holder_growth" in example
        for example in calls["examples"]
    )

    row = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == "parity")
        .order_by(models.SystemHealth.ts.desc())
    )
    assert row.state == "red"


def test_parity_push_cooldown(session, tmp_path, monkeypatch) -> None:
    """A broken lake pages at most once per cooldown window; the health row
    still refreshes every run."""
    settings = _settings(tmp_path)
    asset = _seed_consistent(session, tmp_path, settings)
    _add_sql_only_holder(session, asset)
    session.commit()

    pushes = {"n": 0}

    def fake_notify(
        mismatch_count, compared_assets, decision_ts, examples, *, settings=None
    ):
        pushes["n"] += 1
        return True

    monkeypatch.setattr(parity_module, "notify_parity_mismatch", fake_notify)

    first = run_parity(session, decision_ts=DECISION, settings=settings)
    assert first["pushed"] is True
    assert pushes["n"] == 1

    time.sleep(0.01)  # distinct health-row timestamps (unique (component, ts))
    second = run_parity(session, decision_ts=DECISION, settings=settings)
    assert second["status"] == "red"
    assert second["pushed"] is False
    assert pushes["n"] == 1


def test_parity_disabled_skips(session, tmp_path) -> None:
    settings = _settings(tmp_path, parity_enabled=False)
    result = run_parity(session, decision_ts=DECISION, settings=settings)
    assert result == {"skipped": True}


def test_parity_persists_mismatch_history(session, tmp_path) -> None:
    """Every divergence is recorded as a reviewable history row (run/decision
    timestamps, asset, feature, SQL vs lake values, missing flags, state), and
    stale history older than the retention window is pruned on the next run."""
    settings = _settings(tmp_path, parity_history_retention_days=30)
    asset = _seed_consistent(session, tmp_path, settings)
    _add_sql_only_holder(session, asset)
    old = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    session.add(
        models.ParityMismatch(
            run_ts=old,
            decision_ts=old,
            asset_id=asset.id,
            symbol=asset.symbol,
            feature_name="holder_count",
            sql_value=8.0,
            lake_value=7.0,
            sql_missing=False,
            lake_missing=False,
            state="red",
        )
    )
    session.commit()

    result = run_parity(session, decision_ts=DECISION, settings=settings)
    session.commit()
    assert result["status"] == "red"
    rows = session.scalars(
        select(models.ParityMismatch).order_by(models.ParityMismatch.run_ts)
    ).all()
    assert rows, "divergences must persist as history rows"
    assert all(ensure_utc(row.run_ts) > old for row in rows), (
        "stale history must be pruned"
    )
    by_feature = {row.feature_name: row for row in rows}
    # The SQL-only holder diverges on holder_count (and holder_growth).
    row = by_feature.get("holder_count")
    assert row is not None
    assert row.asset_id == asset.id
    assert row.symbol == asset.symbol
    # The SQL-only holder is a VALUE divergence (SQL sees 8 holders, the lake
    # reconstructs 7 from the archived evidence) — not a missing-flag split.
    assert row.sql_missing is False
    assert row.lake_missing is False
    assert row.sql_value == pytest.approx(8.0)
    assert row.lake_value == pytest.approx(7.0)
    assert row.state == "red"
    assert ensure_utc(row.decision_ts) == DECISION


def test_parity_ok_run_leaves_no_mismatch_rows(session, tmp_path) -> None:
    """A clean run persists no divergence rows."""
    settings = _settings(tmp_path)
    _seed_consistent(session, tmp_path, settings)
    session.commit()
    result = run_parity(session, decision_ts=DECISION, settings=settings)
    session.commit()
    assert result["status"] == "ok"
    assert not session.scalars(select(models.ParityMismatch)).all()


def test_parity_builds_lake_block_once_per_run(session, tmp_path, monkeypatch) -> None:
    """The full-lake parity job reconstructs the lake-covered block for ALL
    assets in ONE ``build_for_assets`` call (one download / one DuckDB
    session) instead of per-asset ``build_for_asset`` calls."""
    settings = _settings(tmp_path)
    _seed_consistent(session, tmp_path, settings)
    session.commit()

    calls = {"n": 0, "addresses": None}
    real = parity_module.LakeFeatureFactory.build_for_assets

    def counting_batch(self, addresses, decision_ts):
        calls["n"] += 1
        calls["addresses"] = list(addresses)
        return real(self, addresses, decision_ts)

    monkeypatch.setattr(
        parity_module.LakeFeatureFactory, "build_for_assets", counting_batch
    )
    result = run_parity(session, decision_ts=DECISION, settings=settings)
    assert calls["n"] == 1
    # Both the base token and its quote asset are compared in the one call.
    assert calls["addresses"] is not None and len(calls["addresses"]) >= 2
    assert result["compared_assets"] == len(calls["addresses"])
    assert result["status"] in ("ok", "yellow", "red")


def test_parity_due_gate(session, tmp_path) -> None:
    """The cadence gate: due with no run yet, quiet right after a run, due
    again once the frequency has elapsed."""
    settings = _settings(tmp_path)
    assert parity_due(session, now=utc_now(), settings=settings) is True
    _seed_consistent(session, tmp_path, settings)
    session.commit()
    run_parity(session, decision_ts=DECISION, settings=settings)
    session.flush()
    assert parity_due(session, now=utc_now(), settings=settings) is False
    assert (
        parity_due(
            session,
            now=utc_now() + timedelta(hours=25),
            settings=settings,
        )
        is True
    )
    # Disabled -> never due.
    assert (
        parity_due(
            session, now=utc_now(), settings=_settings(tmp_path, parity_enabled=False)
        )
        is False
    )


def test_parity_decision_ts_clamps_to_archive_horizon() -> None:
    """The comparison decision time is floored to the hour and clamped so the
    archived lake provably covers every piece of evidence at that time."""
    base = Settings(_env_file=None)
    now = DECISION

    # A configured horizon smaller than the archive horizon is clamped up.
    small = Settings(_env_file=None, parity_compare_hours_ago=48.0)
    dt = parity_decision_ts(small, now)
    assert dt.minute == 0 and dt.second == 0 and dt.microsecond == 0
    assert (now - dt) >= timedelta(
        hours=base.archive_compact_after_hours + base.retention_cadence_hours + 1
    )

    # A configured horizon beyond the archive horizon is honored.
    large = Settings(_env_file=None, parity_compare_hours_ago=200.0)
    assert (now - parity_decision_ts(large, now)) >= timedelta(hours=200)


def test_notify_parity_mismatch_disabled_and_post(monkeypatch) -> None:
    """ntfy parity paging: disabled -> False; enabled -> posts and True."""
    disabled = Settings(_env_file=None, ntfy_enabled=False, ntfy_topic="t")
    assert (
        notify_parity_mismatch(
            3, 10, DECISION, ["x [y]: sql=1.0 lake=2.0"], settings=disabled
        )
        is False
    )

    sent: dict = {}

    def fake_post(self, message, headers):
        sent.update({"message": message, "headers": headers})

    monkeypatch.setattr("ops.notifier.NtfyNotifier._post", fake_post)
    enabled = Settings(
        _env_file=None, ntfy_enabled=True, ntfy_topic="serpent-test"
    )
    assert (
        notify_parity_mismatch(
            3, 10, DECISION, ["x [y]: sql=1.0 lake=2.0"], settings=enabled
        )
        is True
    )
    assert "3 mismatches across 10" in sent["message"]
    assert sent["headers"]["Title"] == "Serpent Circle - Lake Parity Mismatch"


def test_latest_parity_reads_last_run(session) -> None:
    """latest_parity returns None before any run, then the structured summary
    (state, mismatch count, compared assets, decision time, tolerance) parsed
    from the latest parity health row."""
    from storage.repository import record_health

    assert parity_module.latest_parity(session) is None
    decision = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    record_health(
        session,
        component="parity",
        state="yellow",
        message=(
            f"lake-vs-SQL parity: 2 mismatches across 10 assets at decision "
            f"{decision.isoformat()}; tolerance=0.001"
        ),
        error_count=0,
    )
    session.commit()
    latest = parity_module.latest_parity(session)
    assert latest is not None
    assert latest["state"] == "yellow"
    assert latest["mismatch_count"] == 2
    assert latest["compared_assets"] == 10
    assert latest["decision_ts"] == decision
    assert latest["tolerance"] == pytest.approx(0.001)
    assert latest["compare_hours_ago"] == 96.0
