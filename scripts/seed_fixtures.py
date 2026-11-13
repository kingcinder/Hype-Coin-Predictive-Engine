from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.time import utc_now
from scoring.engine import score_current_assets
from storage import models
from storage.database import Base, session_scope
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_holder_once,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    record_health,
    store_raw_evidence,
    upsert_asset,
    upsert_contract,
    upsert_pool_and_pair,
)


def seed_fixture_data(*, session: Session | None = None) -> None:
    """Seed the 2-token fixture fleet (assets, snapshots, holders, scores).

    ``session`` is injectable for tests: when a caller passes one, schema
    creation and all writes go through it and its lifecycle stays with the
    caller. When None — the CLI path — this call opens and owns its own
    ``session_scope()`` cycle, mirroring ``scripts/rescore.py``'s seam (the
    seam can never be reached from ``__main__``, which passes no session).
    """
    if session is not None:
        _seed_in_session(session)
        return
    with session_scope() as active:
        _seed_in_session(active)


def _seed_in_session(session: Session) -> None:
    """The fixture-seeding body against an active session (commits at the end)."""
    Base.metadata.create_all(bind=session.get_bind())
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
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
        address="USDC111111111111111111111111111111111111",
        symbol="USDC",
        name="USD Coin",
        first_seen_at=now - timedelta(days=365),
    )
    candidates = [
        ("HYPE", "TokenHype111111111111111111111111111111", 120_000, [1.0, 1.04, 1.20], 0.15),
        ("DANGER", "TokenDanger1111111111111111111111111111", 4_000, [1.0, 1.8, 2.4], 0.92),
    ]
    asset_ids: list[int] = []
    for symbol, address, liquidity, prices, concentration in candidates:
        asset = upsert_asset(
            session,
            chain_id=chain.id,
            address=address,
            symbol=symbol,
            name=f"{symbol} fixture token",
            first_seen_at=now - timedelta(hours=3),
            website_url="https://example.org" if symbol == "HYPE" else None,
            github_url="https://github.com/example/hype" if symbol == "HYPE" else None,
        )
        contract = upsert_contract(
            session,
            chain_id=chain.id,
            asset_id=asset.id,
            address=address,
            observed_at=now - timedelta(hours=3),
            deployer_wallet=f"deployer-{symbol.lower()}",
        )
        pool, pair = upsert_pool_and_pair(
            session,
            chain_id=chain.id,
            dex_id="raydium",
            pair_address=f"Pair{symbol}111111111111111111111111111111",
            base_asset_id=asset.id,
            quote_asset_id=quote.id,
            created_at_source=now - timedelta(hours=3),
        )
        for idx, price in enumerate(prices):
            ts = now - timedelta(hours=2 - idx)
            insert_market_snapshot_once(
                session,
                pair_id=pair.id,
                source_id=source.id,
                ts=ts,
                observed_at=ts,
                price_usd=price,
                volume_usd=5_000 * (idx + 1) * (4 if symbol == "DANGER" else 1),
                buys=20 + idx * 15,
                sells=5 + idx,
                trades=25 + idx * 16,
            )
            insert_liquidity_snapshot_once(
                session,
                pool_id=pool.id,
                source_id=source.id,
                ts=ts,
                observed_at=ts,
                reserve_usd=liquidity,
            )
        if symbol == "DANGER":
            # Idempotent on the unique key (contract_id, ts, flag_type,
            # source_id): re-running the seed must not duplicate rows.
            flag_exists = session.scalar(
                select(models.ContractFlag.id).where(
                    models.ContractFlag.contract_id == contract.id,
                    models.ContractFlag.ts == now,
                    models.ContractFlag.flag_type == "mint_or_freeze_danger",
                    models.ContractFlag.source_id == source.id,
                )
            )
            if flag_exists is None:
                session.add(
                    models.ContractFlag(
                        contract_id=contract.id,
                        source_id=source.id,
                        ts=now,
                        observed_at=now,
                        flag_type="mint_or_freeze_danger",
                        severity="critical",
                        details={"fixture": True},
                    )
                )
        # Idempotent on the unique key (asset_id, wallet_address, ts,
        # source_id): re-running the seed must not duplicate rows.
        holder_exists = session.scalar(
            select(models.Holder.id).where(
                models.Holder.asset_id == asset.id,
                models.Holder.wallet_address == f"wallet-{symbol}-top",
                models.Holder.ts == now,
                models.Holder.source_id == source.id,
            )
        )
        if holder_exists is None:
            session.add(
                models.Holder(
                    asset_id=asset.id,
                    wallet_address=f"wallet-{symbol}-top",
                    source_id=source.id,
                    ts=now,
                    observed_at=now,
                    balance=1_000_000,
                    pct_supply=concentration,
                )
            )
        asset_ids.append(asset.id)
    score_current_assets(session, decision_ts=now, asset_ids=asset_ids)
    # The fixture health row is a seed marker, not a log: replace it on
    # re-runs instead of duplicating it.
    session.execute(
        delete(models.SystemHealth).where(models.SystemHealth.component == "fixture_seed")
    )
    record_health(
        session, component="fixture_seed", state="ok", message="fixture scores created", ts=now
    )
    session.commit()


def _pool_payload(
    *,
    pair_address: str,
    base_address: str,
    price: float,
    volume: float,
    liquidity: float,
    buys: int,
    sells: int,
    created_at: datetime,
) -> dict[str, object]:
    """One GeckoTerminal ``new_pools`` item, shaped like the live API payload.

    The lake read path parses exactly this shape, and the ``h1`` window is the
    first precedence tier, so the numbers here are the same ones the SQL
    snapshots are written with — that is what makes the parity comparison
    meaningful rather than vacuous.
    """
    stamp = created_at.isoformat().replace("+00:00", "Z")
    return {
        "id": f"solana_{pair_address}",
        "type": "pool",
        "attributes": {
            "address": pair_address,
            "name": "FIXTURE / WETH",
            "pool_created_at": stamp,
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
            "base_token": {"data": {"id": f"solana_{base_address}", "type": "token"}},
            "quote_token": {
                "data": {
                    "id": "solana_So11111111111111111111111111111111111111112",
                    "type": "token",
                }
            },
            "dex": {"data": {"id": "solana_raydium", "type": "dex"}},
        },
    }


def seed_parity_window(
    *, session: Session | None = None, settings: Settings | None = None
) -> dict[str, object]:
    """Seed one fully evidence-backed asset at the lake-vs-SQL comparison hour.

    This is the archive/parity CI fixture. Every normalized row it writes has
    the raw evidence payload that produced it, archived for the *same* hour as
    the parity decision time, so:

    * the evidence is older than ``ARCHIVE_COMPACT_AFTER_HOURS``, giving the
      archive pass due partitions to compact, and
    * both read paths compare the same observations, giving parity a non-zero
      comparison with zero mismatches (a matching divergence would mean the
      fixture is wrong, not that the read paths disagree).

    The asset is a single healthy token plus the quote asset; flags, holders and
    the market series all carry their evidence, because the lake path
    reconstructs every lake-covered feature from those payloads.
    """
    from ops.parity import parity_decision_ts

    settings = settings or get_settings()
    decision_ts = parity_decision_ts(settings, utc_now())

    if session is not None:
        summary = _seed_parity_in_session(session, decision_ts)
    else:
        with session_scope() as active:
            summary = _seed_parity_in_session(active, decision_ts)
    summary["decision_ts"] = decision_ts.isoformat()
    summary["archive_compact_after_hours"] = settings.archive_compact_after_hours
    return summary


def _seed_parity_in_session(session: Session, decision_ts: datetime) -> dict[str, object]:
    Base.metadata.create_all(bind=session.get_bind())
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
    analysis_source = get_or_create_source(
        session, name="contract_analysis", source_type="chain_rpc", tier="chain", base_url=None
    )
    address = "TokenParity11111111111111111111111111111111"
    pair_address = "PairParity1111111111111111111111111111111111"
    created = decision_ts - timedelta(hours=6)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address=address,
        symbol="PARITY",
        name="Parity fixture token",
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
    pool, pair = upsert_pool_and_pair(
        session,
        chain_id=chain.id,
        dex_id="raydium",
        pair_address=pair_address,
        base_asset_id=asset.id,
        quote_asset_id=quote.id,
        created_at_source=created,
    )
    pool.created_at = created
    pair.created_at = created
    contract = upsert_contract(
        session, chain_id=chain.id, asset_id=asset.id, address=address, observed_at=created
    )
    contract.created_at = created

    evidence_rows = 0

    def observe_market(
        observed: datetime, price: float, volume: float, buys: int, sells: int, liquidity: float
    ) -> object:
        """Write one market observation to both paths: evidence + snapshots."""
        nonlocal evidence_rows
        raw = store_raw_evidence(
            session,
            source=gt,
            payload={
                "chain": "solana",
                "new_pools": [
                    _pool_payload(
                        pair_address=pair_address,
                        base_address=address,
                        price=price,
                        volume=volume,
                        liquidity=liquidity,
                        buys=buys,
                        sells=sells,
                        created_at=created,
                    )
                ],
            },
            observed_at=observed,
        )
        evidence_rows += 1
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
            pool_id=pool.id,
            source_id=gt.id,
            ts=ts,
            observed_at=observed,
            reserve_usd=liquidity,
            raw_evidence_id=raw.id,
        )
        return raw

    # Price arc over the trailing two hours.
    for hours_ago, price, volume, buys, sells in (
        (2, 1.10, 7_000.0, 18, 7),
        (1, 1.25, 18_000.0, 38, 14),
    ):
        observe_market(
            decision_ts - timedelta(hours=hours_ago), price, volume, buys, sells, 120_000.0
        )

    # One scan below the discovery-liquidity threshold: the SQL path stores a
    # low_liquidity flag and the lake path reconstructs the same one. It lands
    # in its own hour so neither path has to arbitrate two rows in one hour.
    low_observed = decision_ts - timedelta(hours=3, minutes=30)
    raw_low = observe_market(low_observed, 0.50, 100.0, 1, 1, 900.0)
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

    # Contract analyzer pass: the SQL path stores one ContractFlag per finding,
    # the lake path counts the same findings out of this payload.
    ca_observed = decision_ts - timedelta(hours=4)
    findings = [
        {"flag_type": "mint_authority", "severity": "warning"},
        {"flag_type": "ownership_not_renounced", "severity": "warning"},
    ]
    raw_ca = store_raw_evidence(
        session,
        source=analysis_source,
        payload={
            "contract_analysis": {
                "chain": "solana",
                "asset_address": address,
                "suspicious_flags": len(findings),
                "findings": findings,
            }
        },
        observed_at=ca_observed,
    )
    evidence_rows += 1
    for finding in findings:
        session.add(
            models.ContractFlag(
                contract_id=contract.id,
                source_id=analysis_source.id,
                ts=ca_observed,
                observed_at=ca_observed,
                flag_type=finding["flag_type"],
                severity=finding["severity"],
                evidence_id=raw_ca.id,
                details=dict(finding),
            )
        )

    # Holder snapshots: two hours of history so holder_growth is defined, with
    # the RPC payloads the lake path dedupes exactly like insert_holder_once.
    supply = 10_000.0
    first_accounts = [("wallet-a", 100.0), ("wallet-b", 200.0), ("wallet-c", 300.0)]
    second_accounts = first_accounts + [("wallet-d", 400.0), ("wallet-e", 500.0)]
    for offset, accounts in (
        (timedelta(hours=2), first_accounts),
        (timedelta(0), second_accounts),
    ):
        observed = decision_ts - offset
        store_raw_evidence(
            session,
            source=rpc,
            payload={
                "asset_id": asset.id,
                "mint": address,
                "supply": supply,
                "largest_accounts": [
                    {"address": wallet, "uiAmountString": str(balance)}
                    for wallet, balance in accounts
                ],
            },
            observed_at=observed,
        )
        evidence_rows += 1
        ts = observed.replace(minute=0, second=0, microsecond=0)
        for wallet, balance in accounts:
            insert_holder_once(
                session,
                asset_id=asset.id,
                wallet_address=wallet,
                source_id=rpc.id,
                ts=ts,
                observed_at=observed,
                balance=balance,
                pct_supply=balance / supply,
            )

    session.execute(
        delete(models.SystemHealth).where(models.SystemHealth.component == "fixture_parity_window")
    )
    record_health(
        session,
        component="fixture_parity_window",
        state="ok",
        message=f"parity fixture seeded at decision {decision_ts.isoformat()}",
        ts=decision_ts,
    )
    session.commit()
    return {
        "asset": asset.symbol,
        "evidence_rows": evidence_rows,
        "contract_flags": 3,
        "holder_hours": 2,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the Serpent Circle fixture data")
    parser.add_argument(
        "--parity-window",
        action="store_true",
        help=(
            "seed the evidence-backed asset at the lake-vs-SQL parity decision hour "
            "(the archive/parity CI fixture) instead of the demo fleet"
        ),
    )
    args = parser.parse_args()
    if args.parity_window:
        print(json.dumps(seed_parity_window(), indent=2, default=str))
    else:
        seed_fixture_data()
        print("Seeded fixture assets, snapshots, features, scores, alerts, and health.")
