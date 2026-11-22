from __future__ import annotations

import threading
from datetime import timedelta
from functools import partial
from time import sleep
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from catalyst.extractor import alert_upcoming_catalysts, extract_catalysts
from common.config import get_settings, secret_value
from common.logging import get_logger
from common.time import ensure_utc, floor_to_hour, utc_now
from common.urls import redact_url
from fingerprint.engine import FingerprintEngine
from forecast.engine import run_forecast_if_due
from ingestion.birdeye_client import BirdeyeClient
from ingestion.contract_analyzer import ContractAnalysis, analyze_contract
from ingestion.data_quality import check_market_snapshots
from ingestion.evm_swaps import fetch_evm_pool_swaps, is_evm_address
from ingestion.holder_tracker import (  # noqa: F401
    get_evm_holders,
    get_solana_holders,
    record_holder_metrics,
)
from ingestion.normalizers import (
    NormalizedPair,
    extract_profile_links,
    normalize_dexscreener_pair,
    normalize_geckoterminal_pool,
    normalize_geckoterminal_trades,
)
from ingestion.provider_gate import RATE_LIMIT_STATUSES, CircuitOpen, retry_after_seconds
from ingestion.rpc_pool import (
    POOL_CHAINS,
    get_rpc_pool,
    persist_pool_snapshots,
    record_pool_health,
)
from ingestion.solana_safety import collect_asset_safety
from ingestion.solana_swaps import fetch_pool_swaps
from ingestion.source_clients import (
    DexScreenerClient,
    EVMHolderClient,
    GeckoTerminalClient,
    SolanaRpcClient,
    get_rpc_url,
    probe_rpc_endpoint,
)
from mempool import run_mempool
from narrative import run_narrative
from ops.notifier import notify_rpc_pool_event, run_notifier
from pump_physics import run_lifecycle
from radar.ignition import IgnitionRadar
from radar.liquidity import LiquidityRemovalWatcher
from radar.prelaunch import PrelaunchQueue
from scoring.engine import score_current_assets
from storage import models
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    insert_holder_once,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    record_health,
    record_scan_result,
    store_raw_evidence,
    upsert_asset,
    upsert_contract,
    upsert_pool_and_pair,
)

log = get_logger(__name__)


class ScanInterrupted(Exception):
    """Raised between scan stages when the engine is shutting down."""


def _int_from_result(value: Any) -> int:
    """Safely extract an integer count from a pipeline stage result.

    Pipeline stages return different shapes:
    - lifecycle: {events: 86, assets: 86}
    - narrative: {reddit: 0, github: 30, ...}
    - mempool: {solana: {watched: 5, ...}, evm: {pairs: 0, ...}}
    - lp_removals: {events: 0, lp_burns: 0, ...}
    - archive: {compacted: 0, partitions: 0, ...}
    """
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, dict):
        # lifecycle / lp_removals: prefer 'events' key
        if "events" in value:
            return int(value["events"])
        # mempool: sum 'watched' across chains
        if "watched" in value or any(isinstance(v, dict) for v in value.values()):
            total = 0
            for v in value.values():
                if isinstance(v, dict):
                    total += int(v.get("watched", 0))
            if total > 0:
                return total
        # archive: use 'partitions' or 'compacted'
        if "partitions" in value:
            return int(value["partitions"])
        # narrative: sum all numeric values (source counts)
        numeric_sum = sum(v for v in value.values() if isinstance(v, int | float))
        if numeric_sum > 0:
            return int(numeric_sum)
        # fallback: try 'count' key
        return int(value.get("count", 0))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _interleave_by_chain(
    items: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Round-robin interleave of per-chain pool lists, preserving each chain's order.

    New pools are ingested chain by chain in ``target_chains`` order, so the
    shared GeckoTerminal rate limit (about 5 trades fetches per scan) used to
    open while Solana pools were still being stored, and Base/Ethereum pools
    were never reached. Interleaving spreads the first fetches fairly across
    chains instead of giving them all to the first chain.
    """
    by_chain: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for chain_slug, payload in items:
        if chain_slug not in by_chain:
            by_chain[chain_slug] = []
            order.append(chain_slug)
        by_chain[chain_slug].append(payload)
    out: list[tuple[str, dict[str, Any]]] = []
    index = 0
    while True:
        progressed = False
        for chain_slug in order:
            pool_list = by_chain[chain_slug]
            if index < len(pool_list):
                out.append((chain_slug, pool_list[index]))
                progressed = True
        if not progressed:
            break
        index += 1
    return out


class IngestionService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._first_buy_stats: dict[str, int] = {}
        self._reset_first_buy_stats()

    def _reset_first_buy_stats(self) -> None:
        self._first_buy_provider_backoff = False
        self._first_buy_stats = {
            "pools": 0,
            # Fetches per chain this scan. The cap is per chain, not global:
            # a global cap is consumed in processing order, so the first chain
            # (Solana) took every slot and EVM pools were never fetched.
            "pools_by_chain": {},
            "budget_skipped": 0,
            "backoff_skipped": 0,
            "gecko_failures": 0,
            "provider_failures": 0,
            "rate_limit_failures": 0,
            "circuit_open_failures": 0,
            "rpc_fallback_hits": 0,
            "rpc_fallback_swaps": 0,
            "evm_rpc_fallback_hits": 0,
            "evm_rpc_fallback_swaps": 0,
            "catchup_pools": 0,
            "catchup_observed": 0,
            "catchup_swaps": 0,
            "swaps": 0,
            "tracked_buys": 0,
            "pending_copy_logs": 0,
            "paper_only_copy_logs": 0,
            "other_copy_logs": 0,
        }

    def ensure_reference_data(self, session: Session) -> None:
        for slug, name, vm_type, native_symbol in [
            ("solana", "Solana", "solana", "SOL"),
            ("base", "Base", "evm", "ETH"),
            ("ethereum", "Ethereum", "evm", "ETH"),
        ]:
            get_or_create_chain(
                session, slug, name=name, vm_type=vm_type, native_symbol=native_symbol
            )
        get_or_create_source(
            session,
            name="dexscreener",
            source_type="market_data",
            tier="venue",
            base_url="https://api.dexscreener.com",
        )
        get_or_create_source(
            session,
            name="geckoterminal",
            source_type="market_data",
            tier="venue",
            base_url="https://api.geckoterminal.com",
        )
        get_or_create_source(
            session,
            name="solana_rpc",
            source_type="chain_rpc",
            tier="chain",
            base_url=get_rpc_url("solana"),
        )
        get_or_create_source(
            session, name="evm_rpc", source_type="chain_rpc", tier="chain", base_url=None
        )
        get_or_create_source(
            session,
            name="evm_holders",
            source_type="chain_rpc",
            tier="chain",
            base_url=None,
        )
        get_or_create_source(
            session,
            name="contract_analysis",
            source_type="chain_rpc",
            tier="chain",
            base_url=None,
        )
        get_or_create_source(
            session,
            name="birdeye",
            source_type="market_data",
            tier="venue",
            base_url="https://public-api.birdeye.so",
        )
        for name, source_type in [
            ("reddit_public", "social"),
            ("youtube_rss", "social"),
            ("github_public", "public_metadata"),
            ("huggingface", "public_metadata"),
            ("rss_news", "news"),
        ]:
            get_or_create_source(
                session, name=name, source_type=source_type, tier="public_metadata"
            )

    @staticmethod
    def _commit_stage(session: Session, stop: threading.Event | None = None) -> None:
        """End the scan's current transaction so SQLite's write lock is released.

        SQLite allows exactly one writer, and a transaction keeps that lock from
        its first write until COMMIT/ROLLBACK. A scan mixes network I/O with DB
        writes, so holding one transaction for the whole scan blocks every other
        writer: the engine's write queue (``system_health`` rows for the sources)
        waits out ``sqlite_busy_timeout_ms`` and drops each write with
        ``database is locked``, and ``system_health`` effectively freezes at boot.
        If a source hangs, the transaction is never committed at all.

        Every stage (and every item of a network-bound loop) therefore ends its
        own unit of work. The scan is a collection of idempotent upserts, so
        persisting a completed stage without waiting for the whole scan is the
        intended trade-off; it is also what makes the scan survivable at all.
        """
        session.commit()
        # Between stages is where a shutdown request is honoured: the stage that
        # just finished is persisted, and the next one may sit in a network call
        # for a minute or more.
        if stop is not None and stop.is_set():
            raise ScanInterrupted

    def run_once(
        self,
        session: Session,
        *,
        pre_scan_health: dict[str, object] | None = None,
        stop: threading.Event | None = None,
    ) -> dict[str, Any]:
        self.ensure_reference_data(session)
        # Reference rows are written before the first network call; commit them
        # so the scan never opens its write transaction during a slow fetch.
        # No stop check here: this call sits outside the try, and the first
        # stage boundary inside it honours the request a moment later.
        self._commit_stage(session)
        self._reset_first_buy_stats()
        errors: list[str] = []
        result: dict[str, Any] = {
            "pairs": 0,
            "dexscreener_refresh_pairs": 0,
            "profiles": 0,
            "scores": 0,
            "ignition_events": 0,
            "fingerprints": 0,
            "forecasts": 0,
            "errors": errors,
        }
        try:
            decision_ts = utc_now()
            result["profiles"] = self._ingest_dexscreener_profiles(session)
            self._commit_stage(session, stop)
            result["pairs"] += self._ingest_dexscreener_boosts(session)
            result["pairs"] += self._ingest_geckoterminal_new_pools(session)
            result["evm_first_buy_catchup"] = self._catch_up_evm_first_buys(session)
            refresh_pairs = self._ingest_dexscreener_refresh(session)
            result["pairs"] += refresh_pairs
            result["dexscreener_refresh_pairs"] = refresh_pairs
            self._record_first_buy_health(session)
            self._commit_stage(session, stop)
            result["birdeye_tokens"] = self._ingest_birdeye_solana(session)
            self._commit_stage(session, stop)
            result["holder_snapshots"] = self._ingest_solana_holder_snapshots(session)
            result["safety_snapshots"] = self._ingest_solana_safety_snapshots(session)
            result["holder_snapshots"] += self._ingest_evm_holder_snapshots(session)
            self._commit_stage(session, stop)
            result["mempool"] = run_mempool(session, decision_ts=decision_ts)
            result["lp_removals"] = LiquidityRemovalWatcher().scan(session, decision_ts=decision_ts)
            result["prelaunch"] = len(PrelaunchQueue().scan(session, decision_ts=decision_ts))
            self._commit_stage(session, stop)
            result["narrative"] = run_narrative(session, decision_ts=decision_ts)
            result["catalysts"] = extract_catalysts(session, decision_ts=decision_ts)
            result["catalyst_alerts"] = alert_upcoming_catalysts(session, decision_ts=decision_ts)
            self._commit_stage(session, stop)
            radar_result = IgnitionRadar().scan(session, decision_ts=decision_ts)
            result["ignition_events"] = sum(radar_result.values())
            fingerprint = FingerprintEngine()
            result["fingerprint_clusters"] = fingerprint.learn(session)
            result["fingerprints"] = len(fingerprint.assess(session))
            self._commit_stage(session, stop)
            result["lifecycle"] = run_lifecycle(session, decision_ts=decision_ts)
            self._commit_stage(session, stop)
            forecast_result = run_forecast_if_due(session, decision_ts=decision_ts)
            result["forecasts"] = forecast_result.get("forecasts", 0)
            self._commit_stage(session, stop)
            scores = score_current_assets(session, decision_ts=decision_ts)
            result["scores"] = len(scores)
            self._commit_stage(session, stop)
            # ── Risk calibration: learn from historical outcomes ─────────
            try:
                from risk_engine.calibrator import run_calibration

                cal_result = run_calibration(session, decision_ts=decision_ts)
                result["risk_calibration"] = {
                    "version": cal_result.version,
                    "adjusted": cal_result.adjusted,
                    "sample_size": cal_result.sample_size,
                }
            except Exception as cal_exc:  # noqa: BLE001
                log.debug("risk_calibration_failed", error=str(cal_exc))
            self._commit_stage(session, stop)
            result["ntfy"] = run_notifier(session, decision_ts=decision_ts)
            self._commit_stage(session, stop)
            # Compaction is owned by the retention autopilot on its cadence
            # (RETENTION_CADENCE_HOURS), running on a per-partition schedule.
            # The scan never touches the archive; it just reports the stage as
            # skipped (partitions=0) so the ops console shows archive=0.
            result["archive"] = {"skipped": True, "partitions": 0, "compacted": 0}
            # ── Phase 1: Data quality check ─────────────────────────────
            try:
                quality = self._run_data_quality_check(session, decision_ts)
                result["data_quality"] = quality
            except Exception as qe:  # noqa: BLE001
                log.debug("data_quality_check_failed", error=str(qe))
                result["data_quality"] = {"error": str(qe)}
            self._commit_stage(session, stop)
            # ── Phase 1: Contract analysis for new assets ───────────────
            try:
                contract_result = self._run_contract_analysis(session, decision_ts)
                result["contract_analysis"] = contract_result
            except Exception as ce:  # noqa: BLE001
                safe_error = redact_url(str(ce))
                log.debug("contract_analysis_failed", error=safe_error)
                record_health(
                    session,
                    component="source:contract_analysis",
                    state="red",
                    message=f"contract analysis failed: {safe_error}",
                    error_count=1,
                )
                result["contract_analysis"] = {"error": safe_error}
            self._commit_stage(session, stop)
            record_health(
                session,
                component="worker",
                state="ok",
                message=(
                    "scan, mempool, prelaunch, narrative, catalyst, radar, fingerprint, "
                    "lifecycle, forecast, and scoring completed; archive is owned by "
                    "the retention autopilot"
                ),
            )
            # Commit before the RPC pool probes below, which are network calls;
            # the commit above already honours a pending stop.
            self._commit_stage(session, stop)
            # Probe downed endpoints so the health rows reflect fresh state
            # (the background threads cover long-running workers; this covers
            # --once runs and makes recovery deterministic per scan).
            rpc_pool_notifications = 0
            for chain_slug in POOL_CHAINS:
                pool = get_rpc_pool(chain_slug)
                pool.probe_down_endpoints(partial(probe_rpc_endpoint, chain_slug))
                rpc_pool_notifications += pool.dispatch_alerts(
                    notify_rpc_pool_event,
                    cooldown_seconds=self.settings.rpc_pool_alert_cooldown_seconds,
                )
            result["rpc_pool_notifications"] = rpc_pool_notifications
            result["rpc_pool_snapshots"] = persist_pool_snapshots(session)
            record_pool_health(session)
            duration_sec = (utc_now() - decision_ts).total_seconds()
            record_scan_result(
                session,
                ts=decision_ts,
                duration_sec=duration_sec,
                pairs=result.get("pairs", 0),
                profiles=result.get("profiles", 0),
                scores=result.get("scores", 0),
                ignition_events=result.get("ignition_events", 0),
                fingerprints=result.get("fingerprints", 0),
                forecasts=result.get("forecasts", 0),
                lifecycle=_int_from_result(result.get("lifecycle", 0)),
                narrative=_int_from_result(result.get("narrative", 0)),
                mempool=_int_from_result(result.get("mempool", 0)),
                lp_removals=_int_from_result(result.get("lp_removals", 0)),
                prelaunch=result.get("prelaunch", 0),
                catalysts=result.get("catalysts", 0),
                archive=_int_from_result(result.get("archive", 0)),
                ntfy_sent=(
                    result.get("ntfy", {}).get("sent", 0)
                    if isinstance(result.get("ntfy"), dict)
                    else 0
                ),
                rpc_pool_notifications=result.get("rpc_pool_notifications", 0),
                rpc_pool_snapshots=result.get("rpc_pool_snapshots", 0),
                state="ok",
                details={"pre_scan_health": pre_scan_health or {}},
            )
            session.commit()
        except ScanInterrupted:
            # Committed stages stayed; only the in-flight stage is discarded.
            session.rollback()
            result["interrupted"] = True
            log.info("ingestion_scan_interrupted", stage="scan")
        except Exception as exc:  # noqa: BLE001 - must preserve exact source failure.
            session.rollback()
            with session.begin():
                record_health(
                    session,
                    component="worker",
                    state="red",
                    message=redact_url(str(exc)),
                    error_count=1,
                )
                record_scan_result(
                    session,
                    ts=utc_now(),
                    state="red",
                    error_message=redact_url(str(exc)),
                )
            errors.append(redact_url(str(exc)))
            log.exception("ingestion_scan_failed", error=redact_url(str(exc)))
        return result

    def _run_data_quality_check(self, session: Session, decision_ts: Any) -> dict[str, Any]:
        """Run data quality checks on the latest market snapshots."""
        recent_snaps = session.scalars(
            select(models.MarketSnapshot)
            .order_by(desc(models.MarketSnapshot.observed_at))
            .limit(200)
        ).all()
        snap_dicts: list[dict[str, Any]] = []
        for s in recent_snaps:
            snap_dicts.append(
                {
                    "pair_id": s.pair_id,
                    "price_usd": s.price_usd,
                    "ts": s.ts,
                    "source_id": s.source_id,
                    "volume_usd": s.volume_usd,
                }
            )
        report = check_market_snapshots(snap_dicts, decision_ts=decision_ts)
        return {
            "checked": report.checked,
            "issues": len(report.issues),
            "stale": report.stale_count,
            "missing": report.missing_count,
            "duplicate": report.duplicate_count,
            "anomalous": report.anomalous_count,
            "ok": report.ok,
        }

    def _run_contract_analysis(self, session: Session, decision_ts: Any) -> dict[str, Any]:
        """Analyze contracts for recently discovered assets, skipping already-analyzed ones.

        Each analysis is persisted as point-in-time raw evidence (the
        ``contract_analysis`` payload with a deterministic ``findings`` list)
        plus one evidence-backed ``ContractFlag`` row per finding — so both
        the SQL feature count and the lake replay see the honeypot /
        mint-authority / ownership findings, not just low-liquidity scans.
        Repeat scans of the same asset are idempotent: identical evidence
        dedupes on content hash and an existing (contract, flag_type) row is
        never re-created.
        """
        # Find assets that don't yet have a contract_flag record
        analyzed_ids = session.scalars(select(models.ContractFlag.contract_id)).all()
        query = select(models.Asset).order_by(desc(models.Asset.created_at)).limit(10)
        if analyzed_ids:
            query = (
                select(models.Asset)
                .where(models.Asset.id.notin_(analyzed_ids))
                .order_by(desc(models.Asset.created_at))
                .limit(10)
            )
        recent_assets = session.scalars(query).all()
        source = get_or_create_source(
            session, name="contract_analysis", source_type="chain_rpc", tier="chain"
        )
        etherscan_enabled = bool((secret_value(self.settings.etherscan_api_key) or "").strip())
        results: list[dict[str, Any]] = []
        skipped_evm = 0
        for asset in recent_assets:
            chain_obj = self._chain(session, asset.chain_id)
            chain_name = chain_obj.slug if chain_obj else "solana"
            if chain_name in ("ethereum", "base") and not etherscan_enabled:
                # Etherscan V2 rejects unauthenticated calls. Do not turn a
                # missing optional key into a red provider failure: defer just
                # the EVM contracts and still analyze Solana contracts.
                skipped_evm += 1
                continue
            analysis = analyze_contract(
                asset.address,
                chain=chain_name,
            )
            findings = _analysis_findings(analysis)
            raw = store_raw_evidence(
                session,
                source=source,
                payload={
                    "contract_analysis": {
                        "chain": chain_name,
                        "asset_address": asset.address,
                        "suspicious_flags": analysis.suspicious_flags,
                        "is_honeypot": analysis.is_honeypot,
                        "has_mint": analysis.has_mint_function,
                        "has_pause": analysis.has_pause_function,
                        "ownership_renounced": analysis.ownership_renounced,
                        "deployer_known_rug": analysis.deployer_known_rug,
                        "reasons": analysis.reasons,
                        "findings": findings,
                    }
                },
                observed_at=decision_ts,
            )
            contract = upsert_contract(
                session,
                chain_id=asset.chain_id,
                asset_id=asset.id,
                address=asset.address,
                observed_at=decision_ts,
            )
            existing = {
                flag.flag_type
                for flag in session.scalars(
                    select(models.ContractFlag).where(
                        models.ContractFlag.contract_id == contract.id
                    )
                ).all()
            }
            for finding in findings:
                flag_type = finding["flag_type"]
                if flag_type in existing:
                    continue
                session.add(
                    models.ContractFlag(
                        contract_id=contract.id,
                        source_id=source.id,
                        ts=decision_ts,
                        observed_at=decision_ts,
                        flag_type=flag_type,
                        severity=finding["severity"],
                        evidence_id=raw.id,
                        details={"chain": chain_name, **finding},
                    )
                )
                existing.add(flag_type)
            results.append(
                {
                    "asset_id": asset.id,
                    "address": asset.address,
                    "suspicious_flags": analysis.suspicious_flags,
                    "is_honeypot": analysis.is_honeypot,
                    "has_mint": analysis.has_mint_function,
                    "ownership_renounced": analysis.ownership_renounced,
                    "findings": findings,
                    "reasons": analysis.reasons,
                    "error": analysis.error,
                }
            )
            # Each analysis is a network round-trip; commit so the next asset's
            # fetch does not run while we still hold SQLite's write lock.
            self._commit_stage(session)
        flagged = sum(1 for r in results if r["suspicious_flags"] > 0)
        errors = [r["error"] for r in results if r["error"]]
        message = f"{len(results)} analyzed, {flagged} flagged"
        if errors:
            message = f"{message}; {len(errors)} errors; first={errors[0]}"
        if skipped_evm:
            if results:
                message = (
                    f"{message}; skipped {skipped_evm} EVM contracts: ETHERSCAN_API_KEY is not set"
                )
            else:
                message = (
                    f"skipped: {skipped_evm} EVM contracts deferred; ETHERSCAN_API_KEY is not set"
                )
        record_health(
            session,
            component="source:contract_analysis",
            state=(
                "red"
                if errors and len(errors) == len(results) and not skipped_evm
                else ("yellow" if errors or skipped_evm else "ok")
            ),
            message=message,
            error_count=len(errors),
        )
        self._commit_stage(session)
        return {"analyzed": len(results), "flagged": flagged}

    def _ingest_birdeye_solana(self, session: Session) -> int:
        """Ingest new tokens from Birdeye API as a third Solana data source.

        Birdeye is optional and key-gated. Without ``BIRDEYE_API_KEY`` every
        request returns 401, which is a configuration state rather than a
        transient outage: the feed is skipped and reported as disabled instead
        of painting a red failure on every scan. A 429 opens the shared Birdeye
        cooldown (see ``BirdeyeClient``), so sustained free-tier rate limits are
        reported as yellow pacing and deferred instead of repeated red failures.
        """
        if "solana" not in self.settings.target_chains:
            return 0
        get_or_create_source(
            session,
            name="birdeye",
            source_type="market_data",
            tier="venue",
            base_url=BirdeyeClient.BASE_URL,
        )
        source = self._source(session, "birdeye")
        api_key = (secret_value(self.settings.birdeye_api_key) or "").strip()
        if not api_key:
            record_health(
                session,
                component="source:birdeye",
                state="yellow",
                message="disabled: BIRDEYE_API_KEY is not set; feed skipped",
            )
            log.info("birdeye_disabled", reason="BIRDEYE_API_KEY is not set")
            return 0
        client = BirdeyeClient(api_key=api_key)
        count = 0
        try:
            new_tokens = client.new_tokens(limit=20)
            store_raw_evidence(
                session,
                source=source,
                payload={"birdeye_new_tokens": new_tokens},
                observed_at=utc_now(),
            )
            for token in new_tokens:
                address = token.get("address") or ""
                if not address:
                    continue
                upsert_asset(
                    session,
                    chain_id=(
                        solana_chain.id if (solana_chain := self._chain(session, "solana")) else 1
                    ),
                    address=address,
                    symbol=token.get("symbol") or "UNKNOWN",
                    name=token.get("name"),
                    first_seen_at=utc_now(),
                )
                count += 1
            record_health(
                session,
                component="source:birdeye",
                state="ok",
                message=f"{count} new tokens",
            )
        except CircuitOpen as exc:
            # The client's cooldown is still active from a prior 429. This is
            # pacing, not a provider failure: defer the feed until it expires.
            record_health(
                session,
                component="source:birdeye",
                state="yellow",
                message=f"pacing: {exc}; Birdeye feed deferred to a later scan",
            )
            log.info("birdeye_deferred", reason=redact_url(str(exc)))
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "response", None), "status_code", None)
            safe_error = redact_url(str(exc))
            if status == 429:
                header_cooldown = retry_after_seconds(exc)
                cooldown = (
                    self.settings.provider_circuit_break_seconds
                    if header_cooldown is None
                    else header_cooldown
                )
                record_health(
                    session,
                    component="source:birdeye",
                    state="yellow",
                    message=(
                        f"pacing: Birdeye rate limited 429; feed cooling down for "
                        f"{cooldown:g}s and deferred to a later scan"
                    ),
                )
                log.info("birdeye_rate_limited", status=status, cooldown_seconds=cooldown)
            elif status in (401, 403):
                # A rejected key is configuration, not an outage: disable the
                # feed as degraded and name the variable an operator must fix.
                status_label = "Unauthorized" if status == 401 else "Forbidden"
                record_health(
                    session,
                    component="source:birdeye",
                    state="yellow",
                    message=(
                        f"disabled: Birdeye rejected BIRDEYE_API_KEY "
                        f"({status} {status_label}); the key is invalid or lacks access; "
                        "replace it or unset it to skip the feed"
                    ),
                )
                log.warning("birdeye_key_rejected", status=status)
            else:
                record_health(
                    session,
                    component="source:birdeye",
                    state="red",
                    message=safe_error,
                    error_count=1,
                )
                log.warning("birdeye_ingestion_failed", error=safe_error)
        finally:
            client.close()
        return count

    def _source(self, session: Session, name: str) -> models.Source:
        source = session.scalar(select(models.Source).where(models.Source.name == name))
        if not source:
            raise RuntimeError(f"source not seeded: {name}")
        return source

    def _chain(self, session: Session, slug_or_id: str | int) -> models.Chain | None:
        if isinstance(slug_or_id, int):
            return session.get(models.Chain, slug_or_id)
        return session.scalar(select(models.Chain).where(models.Chain.slug == slug_or_id))

    def _ingest_dexscreener_profiles(self, session: Session) -> int:
        source = self._source(session, "dexscreener")
        count = 0
        client = DexScreenerClient()
        observed_at = utc_now()
        try:
            profiles = client.latest_token_profiles()
            store_raw_evidence(
                session, source=source, payload={"profiles": profiles}, observed_at=observed_at
            )
            for item in profiles:
                chain = self._chain(session, str(item.get("chainId") or "").lower())
                token_address = item.get("tokenAddress")
                if not chain or not token_address:
                    continue
                website_url, github_url = extract_profile_links(item)
                upsert_asset(
                    session,
                    chain_id=chain.id,
                    address=str(token_address),
                    symbol=str(item.get("symbol") or "UNKNOWN"),
                    name=item.get("description"),
                    first_seen_at=observed_at,
                    website_url=website_url,
                    github_url=github_url,
                )
                count += 1
            record_health(
                session,
                component="source:dexscreener_profiles",
                state="ok",
                message=f"{count} profiles",
            )
        except Exception as exc:  # noqa: BLE001
            record_health(
                session,
                component="source:dexscreener_profiles",
                state="red",
                message=redact_url(str(exc)),
                error_count=1,
            )
            log.warning("dexscreener_profiles_failed", error=redact_url(str(exc)))
        finally:
            client.close()
        return count

    def _ingest_dexscreener_boosts(self, session: Session) -> int:
        source = self._source(session, "dexscreener")
        count = 0
        client = DexScreenerClient()
        try:
            tokens = client.top_boosts()
            store_raw_evidence(
                session, source=source, payload={"top_boosts": tokens}, observed_at=utc_now()
            )
            # The evidence row lands before the per-token loop: from here on the
            # loop's HTTP call must never run inside an open write transaction.
            session.commit()
            for token in tokens[:100]:
                chain_slug = str(token.get("chainId") or "").lower()
                token_address = token.get("tokenAddress")
                if chain_slug not in self.settings.target_chains or not token_address:
                    continue
                for pair_payload in client.token_pairs(chain_slug, str(token_address)):
                    if self._store_dexscreener_pair(session, source, pair_payload):
                        count += 1
                # Per-token commit: the next token's token_pairs() call is network
                # I/O, and holding SQLite's single write lock across it starved the
                # engine's write queue (stage-level commits alone were too coarse).
                session.commit()
            record_health(
                session, component="source:dexscreener_pairs", state="ok", message=f"{count} pairs"
            )
        except Exception as exc:  # noqa: BLE001
            record_health(
                session,
                component="source:dexscreener_pairs",
                state="red",
                message=redact_url(str(exc)),
                error_count=1,
            )
            log.warning("dexscreener_pairs_failed", error=redact_url(str(exc)))
        finally:
            client.close()
        return count

    def _ingest_dexscreener_refresh(self, session: Session) -> int:
        """Re-fetch known DexScreener pairs so today's feature points price.

        Discovery-only ingestion leaves every known pair to go stale: only
        ~1.5% of feature points ever get a price, so the forecast trainer pool
        is frozen at 2,288 samples. This stage revisits live pairs once a day
        via the token-pairs endpoint (one call per asset, up to 30 pairs).

        Selection is an hour-bucket rotation over assets whose latest
        DexScreener snapshot is inside the alive window (default 72h).
        Ordering purely by snapshot age would starve: a dead pair's snapshot
        never refreshes, so it would sit at the top of the queue forever and
        live pairs would never be reached. The bucket
        (asset_id % 24 == current UTC hour) guarantees every live asset is
        revisited within 24h no matter how many dead assets share the window,
        and dead assets age out of the window after ``alive_hours`` having
        cost at most one call per bucket day. Assets snapshotted within the
        last hour are skipped: snapshots are hourly buckets, so a same-hour
        refresh is a no-op.
        """
        settings = self.settings
        limit = settings.dexscreener_refresh_assets_per_scan
        if limit <= 0:
            return 0
        source = self._source(session, "dexscreener")
        now = utc_now()
        alive_since = now - timedelta(hours=settings.dexscreener_refresh_alive_hours)
        fresh_since = now - timedelta(hours=1)
        last_snap = (
            select(func.max(models.MarketSnapshot.ts))
            .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
            .where(models.Pair.base_asset_id == models.Asset.id)
            .where(models.MarketSnapshot.source_id == source.id)
            .correlate(models.Asset)
            .scalar_subquery()
        )
        rows = session.execute(
            select(
                models.Asset.id,
                models.Chain.slug,
                models.Asset.address,
                last_snap.label("last_snap"),
            )
            .join(models.Chain, models.Chain.id == models.Asset.chain_id)
            .join(models.Pair, models.Pair.base_asset_id == models.Asset.id)
            .where(models.Chain.slug.in_(settings.target_chains))
            .where(models.Asset.id % 24 == now.hour)
            .where(last_snap != None)  # noqa: E711 - renders IS NOT NULL
            .where(last_snap >= alive_since)
            .where(last_snap < fresh_since)
            .group_by(models.Asset.id)
            .order_by(last_snap.asc())
            .limit(limit)
        ).all()
        count = 0
        client = DexScreenerClient()
        try:
            for _asset_id, chain_slug, token_address, _last_snap in rows:
                try:
                    pair_payloads = client.token_pairs(chain_slug, token_address)
                except Exception as exc:  # noqa: BLE001 - one dead token must not stall the stage
                    log.warning(
                        "dexscreener_refresh_token_failed",
                        error=redact_url(str(exc)),
                    )
                    continue
                for pair_payload in pair_payloads:
                    if self._store_dexscreener_pair(session, source, pair_payload):
                        count += 1
                # Per-asset commit: same reason as the boosts loop — the next
                # token_pairs() call is network I/O and must not hold the
                # write lock across it.
                session.commit()
            record_health(
                session,
                component="source:dexscreener_refresh",
                state="ok",
                message=f"{count} pairs from {len(rows)} assets",
            )
        except Exception as exc:  # noqa: BLE001
            record_health(
                session,
                component="source:dexscreener_refresh",
                state="red",
                message=redact_url(str(exc)),
                error_count=1,
            )
            log.warning("dexscreener_refresh_failed", error=redact_url(str(exc)))
        finally:
            client.close()
        return count

    def _ingest_geckoterminal_new_pools(self, session: Session) -> int:
        source = self._source(session, "geckoterminal")
        client = GeckoTerminalClient()
        count = 0
        deferred: list[str] = []
        failed: list[str] = []
        pace_detail: str | None = None
        first_failure: str | None = None
        try:
            # Phase 1 — discover new pools for EVERY chain before storing any
            # of them. Previously discovery and storage were interleaved chain
            # by chain, so a rate limit opened by Solana's first-buy trades
            # fetches aborted the loop and deferred Base/Ethereum discovery to
            # a later scan — by which time the 5-minute first-buy window had
            # closed. Discovery no longer depends on the trades fetches.
            discovered: list[tuple[str, dict[str, Any]]] = []
            for chain_slug in self.settings.target_chains:
                if chain_slug not in ("solana", "base", "ethereum"):
                    continue
                try:
                    items = client.new_pools(chain_slug)
                except CircuitOpen as exc:
                    # Pacing, not a failure: the shared provider gate is inside
                    # its cooldown window. Defer just this chain; the others
                    # still get discovered.
                    deferred.append(chain_slug)
                    if pace_detail is None:
                        pace_detail = str(exc)
                    log.info(
                        "geckoterminal_new_pools_deferred",
                        chain=chain_slug,
                        reason=redact_url(str(exc)),
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in RATE_LIMIT_STATUSES:
                        # The client just opened the shared cooldown. This is
                        # pacing, not a provider outage, so the first 429/403
                        # is yellow too.
                        deferred.append(chain_slug)
                        if pace_detail is None:
                            cooldown = self.settings.provider_circuit_break_seconds
                            pace_detail = (
                                f"GeckoTerminal rate limited ({status}); "
                                f"cooling down for {cooldown:g}s"
                            )
                        log.info(
                            "geckoterminal_new_pools_deferred",
                            chain=chain_slug,
                            status=status,
                            cooldown_seconds=self.settings.provider_circuit_break_seconds,
                        )
                        continue
                    failed.append(chain_slug)
                    if first_failure is None:
                        first_failure = redact_url(str(exc))
                    log.warning(
                        "geckoterminal_new_pools_failed",
                        chain=chain_slug,
                        error=redact_url(str(exc)),
                    )
                    continue
                store_raw_evidence(
                    session,
                    source=source,
                    payload={"chain": chain_slug, "new_pools": items},
                    observed_at=utc_now(),
                )
                discovered.extend((chain_slug, pool_payload) for pool_payload in items)
                # Per-chain commit: storing the next chain's pools is network
                # I/O, so the lock must be released before it starts.
                session.commit()
            # Phase 2 — store round-robin by chain, so the first-buy trades
            # fetches (capped by the shared GeckoTerminal rate limit at about
            # 5 per scan) are spread fairly instead of all going to Solana.
            for _chain_slug, pool_payload in _interleave_by_chain(discovered):
                if self._store_geckoterminal_pool(session, source, pool_payload):
                    count += 1
            session.commit()
        except Exception as exc:  # noqa: BLE001 - storage must not kill the scan.
            failed.append("storage")
            if first_failure is None:
                first_failure = redact_url(str(exc))
            log.warning("geckoterminal_new_pools_failed", error=redact_url(str(exc)))
            # The failed store left the session mid-transaction; roll back so
            # the health row below can still be written.
            session.rollback()
        finally:
            client.close()
        if failed:
            record_health(
                session,
                component="source:geckoterminal_new_pools",
                state="red",
                message=first_failure or "new pools ingestion failed",
                error_count=1,
            )
        elif deferred:
            # Pacing, not a failure: degrade to yellow; red stays reserved for
            # requests the provider actually rejected or that broke.
            record_health(
                session,
                component="source:geckoterminal_new_pools",
                state="yellow",
                message=(
                    f"pacing: {pace_detail}; {count} pools ingested, "
                    f"chains deferred: {', '.join(deferred)}"
                ),
            )
            log.info(
                "geckoterminal_new_pools_deferred",
                reason=pace_detail,
                pools=count,
                deferred=deferred,
            )
        else:
            record_health(
                session,
                component="source:geckoterminal_new_pools",
                state="ok",
                message=f"{count} normalized pools",
            )
        return count

    def _ingest_solana_holder_snapshots(self, session: Session) -> int:
        if "solana" not in self.settings.target_chains:
            return 0
        source = self._source(session, "solana_rpc")
        chain = self._chain(session, "solana")
        if not chain:
            return 0
        assets = self._holder_scan_assets(session, chain.id, self.settings.solana_holder_scan_limit)
        if not assets:
            record_health(
                session,
                component="source:solana_holders",
                state="yellow",
                message="0 eligible assets",
            )
            return 0

        observed_at = utc_now()
        ts = floor_to_hour(observed_at)
        count = 0
        errors: list[str] = []
        client = SolanaRpcClient()
        try:
            for index, asset in enumerate(assets):
                try:
                    supply = client.get_token_supply(asset.address)
                    sleep(max(0.0, self.settings.solana_holder_rpc_pause_seconds))
                    accounts = client.get_token_largest_accounts(asset.address)
                    raw = store_raw_evidence(
                        session,
                        source=source,
                        payload={
                            "asset_id": asset.id,
                            "mint": asset.address,
                            "supply": supply,
                            "largest_accounts": accounts,
                        },
                        observed_at=observed_at,
                    )
                    for account in accounts:
                        address = str(account.get("address") or "")
                        if not address:
                            continue
                        balance = _float_from_rpc(
                            account.get("uiAmountString") or account.get("uiAmount")
                        )
                        if balance is None:
                            continue
                        pct_supply = balance / supply if supply and supply > 0 else None
                        insert_holder_once(
                            session,
                            asset_id=asset.id,
                            wallet_address=address,
                            source_id=source.id,
                            ts=ts,
                            observed_at=observed_at,
                            balance=balance,
                            pct_supply=pct_supply,
                        )
                        count += 1
                    from ingestion.holder_tracker import (  # noqa: PLC0415
                        record_dev_wallet_stats,
                        record_holder_metrics,
                    )

                    pcts = []
                    if supply and supply > 0:
                        pcts = [
                            float(account.get("uiAmount") or 0) / supply
                            for account in accounts
                            if account.get("uiAmount") is not None
                        ]
                    top10 = sum(sorted(pcts, reverse=True)[:10]) if pcts else None
                    record_holder_metrics(
                        session,
                        asset.id,
                        top10_pct=top10,
                        holder_count=len(accounts) if accounts else None,
                        observed_at=observed_at,
                    )
                    contract = session.scalar(
                        select(models.Contract)
                        .where(models.Contract.asset_id == asset.id)
                        .order_by(models.Contract.created_at.desc())
                        .limit(1)
                    )
                    record_dev_wallet_stats(
                        session,
                        asset.id,
                        deployer=contract.deployer_wallet if contract else None,
                        observed_at=observed_at,
                    )
                    raw.raw_path = raw.raw_path or f"holder_snapshot:{asset.id}:{ts.isoformat()}"
                except Exception as exc:  # noqa: BLE001 - preserve per-asset source failure.
                    session.rollback()
                    errors.append(f"{asset.address}: {redact_url(str(exc))}")
                # Per-asset commit: the RPC calls above are network I/O and must
                # not run inside an open write transaction.
                self._commit_stage(session)
                # The wait guards the next asset's first RPC request; there is
                # nothing to pace after the final asset.
                if index + 1 < len(assets):
                    sleep(max(0.0, self.settings.solana_holder_rpc_pause_seconds))
            state = "ok" if not errors else "yellow"
            message = f"{count} holder rows across {len(assets)} assets"
            if errors:
                message = f"{message}; {len(errors)} asset errors; first={errors[0]}"
            record_health(
                session,
                component="source:solana_holders",
                state=state,
                message=message,
                error_count=len(errors),
            )
        except Exception as exc:  # noqa: BLE001
            record_health(
                session,
                component="source:solana_holders",
                state="red",
                message=redact_url(str(exc)),
                error_count=1,
            )
            log.warning("solana_holders_failed", error=redact_url(str(exc)))
        finally:
            client.close()
        return count

    def _ingest_evm_holder_snapshots(self, session: Session) -> int:
        """Holder snapshots for Base/Ethereum assets via GoPlus's keyless
        token-security API (``EVMHolderClient``).

        Stores the SAME evidence shape as the Solana path — ``mint`` / ``supply``
        / ``largest_accounts`` — on the ``evm_holders`` chain_rpc source, so the
        lake replay reconstructs ``holder_count`` / ``holder_growth`` /
        ``top_holder_concentration`` for EVM assets exactly like Solana. One
        aggregate ``source:evm_holders`` health row covers both chains.
        """
        evm_chains = [slug for slug in ("base", "ethereum") if slug in self.settings.target_chains]
        if not evm_chains:
            return 0
        source = get_or_create_source(
            session, name="evm_holders", source_type="chain_rpc", tier="chain"
        )
        count = 0
        errors: list[str] = []
        scanned_chains: list[str] = []
        found_chains: list[str] = []
        first_request = True
        for chain_slug in evm_chains:
            chain = self._chain(session, chain_slug)
            if not chain:
                continue
            found_chains.append(chain_slug)
            limit = max(0, self.settings.evm_holder_scan_limit)
            if limit == 0:
                continue
            assets = self._holder_scan_assets(session, chain.id, limit)
            if not assets:
                continue
            scanned_chains.append(chain_slug)
            observed_at = utc_now()
            ts = floor_to_hour(observed_at)
            client = EVMHolderClient(chain_slug)
            try:
                for asset in assets:
                    # One provider call carries supply, the exact count, and the
                    # ranked list, so pace only before another GoPlus request.
                    # This also covers the gap between chains.
                    if not first_request:
                        sleep(max(0.0, self.settings.evm_holder_rpc_pause_seconds))
                    first_request = False
                    try:
                        snapshot = client.snapshot(asset.address)
                        supply = snapshot.supply
                        accounts = snapshot.holders
                        holder_count = snapshot.holder_count
                        raw = store_raw_evidence(
                            session,
                            source=source,
                            payload={
                                "asset_id": asset.id,
                                "chain": chain_slug,
                                "mint": asset.address,
                                "supply": supply,
                                "holder_count": holder_count,
                                "largest_accounts": accounts,
                            },
                            observed_at=observed_at,
                        )
                        pcts: list[float] = []
                        for account in accounts:
                            address = str(account.get("address") or "")
                            if not address:
                                continue
                            balance = _float_from_rpc(
                                account.get("uiAmountString") or account.get("uiAmount")
                            )
                            if balance is None:
                                continue
                            pct_supply = balance / supply if supply and supply > 0 else None
                            if pct_supply is not None:
                                pcts.append(pct_supply)
                            insert_holder_once(
                                session,
                                asset_id=asset.id,
                                wallet_address=address,
                                source_id=source.id,
                                ts=ts,
                                observed_at=observed_at,
                                balance=balance,
                                pct_supply=pct_supply,
                            )
                            count += 1
                        record_holder_metrics(
                            session,
                            asset.id,
                            top10_pct=(sum(sorted(pcts, reverse=True)[:10]) if pcts else None),
                            holder_count=holder_count,
                            observed_at=observed_at,
                        )
                        raw.raw_path = (
                            raw.raw_path or f"evm_holder_snapshot:{asset.id}:{ts.isoformat()}"
                        )
                    except Exception as exc:  # noqa: BLE001 - per-asset source failure.
                        session.rollback()
                        errors.append(f"{chain_slug}:{asset.address}: {exc}")
                    self._commit_stage(session)
            finally:
                client.close()
        if found_chains:
            state = "ok" if not errors else "yellow"
            if scanned_chains:
                message = f"{count} holder rows across {len(scanned_chains)} chains"
            else:
                state = "yellow"
                message = "0 eligible assets"
            if errors:
                message = f"{message}; {len(errors)} asset errors; first={errors[0]}"
            record_health(
                session,
                component="source:evm_holders",
                state=state,
                message=message,
                error_count=len(errors),
            )
        return count

    def _ingest_solana_safety_snapshots(self, session: Session) -> int:
        """Record mint controls for the same bounded Solana scan cohort."""
        if "solana" not in self.settings.target_chains:
            return 0
        chain = self._chain(session, "solana")
        if chain is None:
            return 0
        assets = self._holder_scan_assets(session, chain.id, self.settings.solana_holder_scan_limit)
        observed_at = utc_now()
        for index, asset in enumerate(assets):
            try:
                collect_asset_safety(session, asset, observed_at=observed_at)
            except Exception as exc:  # noqa: BLE001 - isolate malformed provider/account data.
                session.rollback()
                log.warning(
                    "solana_safety_snapshot_failed",
                    asset_id=asset.id,
                    error=redact_url(str(exc)),
                )
            self._commit_stage(session)
            # Pace only before the next asset's RPC calls; no trailing wait.
            if index + 1 < len(assets):
                sleep(max(0.0, self.settings.solana_holder_rpc_pause_seconds))
        record_health(
            session,
            component="source:solana_safety",
            state="ok" if assets else "yellow",
            message=f"Recorded safety evidence for {len(assets)} Solana assets",
        )
        return len(assets)

    def _holder_scan_assets(
        self, session: Session, chain_id: int, limit: int
    ) -> list[models.Asset]:
        if limit <= 0:
            return []
        rows = session.scalars(
            select(models.Asset)
            .join(models.Pair, models.Pair.base_asset_id == models.Asset.id)
            .where(models.Asset.chain_id == chain_id)
            .order_by(models.Asset.updated_at.desc())
            .limit(limit * 3)
        ).all()
        assets: list[models.Asset] = []
        seen: set[int] = set()
        for asset in rows:
            if asset.id in seen:
                continue
            seen.add(asset.id)
            assets.append(asset)
            if len(assets) >= limit:
                break
        return assets

    def _store_dexscreener_pair(
        self, session: Session, source: models.Source, pair_payload: dict[str, Any]
    ) -> bool:
        normalized = normalize_dexscreener_pair(pair_payload)
        if not normalized:
            return False
        return self._store_normalized_pair(session, source, normalized, pair_payload)

    def _store_geckoterminal_pool(
        self, session: Session, source: models.Source, pool_payload: dict[str, Any]
    ) -> bool:
        normalized = normalize_geckoterminal_pool(pool_payload)
        if not normalized:
            return False
        return self._store_normalized_pair(session, source, normalized, pool_payload)

    def _store_normalized_pair(
        self,
        session: Session,
        source: models.Source,
        normalized: NormalizedPair,
        raw_payload: dict[str, Any],
    ) -> bool:
        chain = self._chain(session, normalized.chain_slug)
        if not chain:
            return False
        observed_at = utc_now()
        # Fetch trades before the first write below, so the provider round-trip
        # never runs while this session holds SQLite's write lock. Earlier
        # pairs from this token's loop may still be pending, so commit them
        # first: SQLite holds the single write lock from the first write until
        # COMMIT, and an uncommitted sibling would otherwise hold it across
        # the GeckoTerminal call.
        session.commit()
        tracked = self._tracked_wallets(normalized.chain_slug)
        swaps, swaps_known = self._first_buy_swaps(normalized, observed_at, tracked)
        first_seen_at = normalized.pair_created_at or observed_at
        base_asset = upsert_asset(
            session,
            chain_id=chain.id,
            address=normalized.base_address,
            symbol=normalized.base_symbol,
            name=normalized.base_name,
            first_seen_at=first_seen_at,
            website_url=normalized.website_url,
            github_url=normalized.github_url,
        )
        quote_asset = None
        if normalized.quote_address:
            quote_asset = upsert_asset(
                session,
                chain_id=chain.id,
                address=normalized.quote_address,
                symbol=normalized.quote_symbol or "QUOTE",
                name=normalized.quote_name,
                first_seen_at=first_seen_at,
            )
        contract = upsert_contract(
            session,
            chain_id=chain.id,
            asset_id=base_asset.id,
            address=normalized.base_address,
            observed_at=observed_at,
        )
        raw = store_raw_evidence(
            session, source=source, payload=raw_payload, observed_at=observed_at
        )
        pool, pair = upsert_pool_and_pair(
            session,
            chain_id=chain.id,
            dex_id=normalized.dex_id,
            pair_address=normalized.pair_address,
            base_asset_id=base_asset.id,
            quote_asset_id=quote_asset.id if quote_asset else None,
            created_at_source=normalized.pair_created_at,
            first_available_at=observed_at,
        )
        ts = floor_to_hour(observed_at)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=observed_at,
            price_usd=normalized.price_usd,
            volume_usd=normalized.volume_usd,
            buys=normalized.buys,
            sells=normalized.sells,
            trades=normalized.trades,
            raw_evidence_id=raw.id,
        )
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=ts,
            observed_at=observed_at,
            reserve_usd=normalized.liquidity_usd,
            reserve_base=normalized.reserve_base,
            reserve_quote=normalized.reserve_quote,
            raw_evidence_id=raw.id,
        )
        if (
            normalized.liquidity_usd is not None
            and normalized.liquidity_usd < self.settings.min_discovery_liquidity_usd
        ):
            flag = models.ContractFlag(
                contract_id=contract.id,
                source_id=source.id,
                ts=observed_at,
                observed_at=observed_at,
                flag_type="low_liquidity",
                severity="warning",
                evidence_id=raw.id,
                details={"liquidity_usd": normalized.liquidity_usd},
            )
            session.add(flag)
        from radar.first_buys import record_first_buys  # noqa: PLC0415 - local import.

        # No trades (old pair, unsupported chain, or provider failure) keeps
        # the stats unknown rather than inventing zeroes.
        summary = record_first_buys(
            session,
            base_asset.id,
            normalized.pair_created_at or observed_at,
            swaps,
            tracked,
            swaps_known=swaps_known,
        )
        for key, value in summary.items():
            self._first_buy_stats[key] = self._first_buy_stats.get(key, 0) + value
        return True

    def _first_buy_swaps(
        self,
        normalized: NormalizedPair,
        observed_at: Any,
        tracked: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Real wallet trades for a pool while its first-buy window is open.

        Returns ``(swaps, observed)``. ``observed`` is False when the provider
        was never asked (window closed, budget/backoff skip) or failed, so the
        caller keeps the stats unknown. A successful empty feed returns
        ``([], True)``: no trades yet is a real answer, not missing data.
        """
        created = normalized.pair_created_at
        if not self.settings.first_buy_trade_fetch_enabled or created is None:
            return [], False
        window = timedelta(minutes=self.settings.first_buy_window_minutes)
        if ensure_utc(observed_at) > ensure_utc(created) + window:
            return [], False
        if normalized.chain_slug not in GeckoTerminalClient.NETWORKS:
            return [], False
        is_evm = normalized.chain_slug in ("base", "ethereum")
        # GeckoTerminal is backed off (rate-limited earlier this scan). Solana
        # pools skip the rest of the scan (existing behaviour: the Solana RPC
        # fallback only triggers on a fresh failure in this call). EVM pools
        # can still use their own RPC -- a different provider, not rate-limited
        # by GeckoTerminal -- so they skip the GeckoTerminal attempt and go
        # straight to the EVM fallback instead of returning unknown.
        gecko_backed_off = self._first_buy_provider_backoff
        if gecko_backed_off and not is_evm:
            self._first_buy_stats["backoff_skipped"] += 1
            return [], False
        if self._first_buy_stats["pools_by_chain"].get(normalized.chain_slug, 0) >= (
            self.settings.first_buy_trade_max_pools_per_scan
        ):
            # Cap provider traffic per chain per scan: first-buy telemetry is
            # useful, but an unbounded pool walk turns rate limits into a
            # warning flood. The cap is per chain so one busy chain cannot
            # starve the others of fetch slots.
            self._first_buy_stats["budget_skipped"] += 1
            return [], False
        self._first_buy_stats["pools_by_chain"][normalized.chain_slug] = (
            self._first_buy_stats["pools_by_chain"].get(normalized.chain_slug, 0) + 1
        )
        self._first_buy_stats["pools"] += 1
        client = GeckoTerminalClient()
        trades: list[dict[str, Any]] = []
        gecko_failed = gecko_backed_off
        try:
            if not gecko_backed_off:
                trades = client.pool_trades(normalized.chain_slug, normalized.pair_address)
        except Exception as exc:  # noqa: BLE001 - fall back to chain data below.
            gecko_failed = True
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(exc, CircuitOpen):
                # The shared flag gates the rest of this scan, but a generic
                # circuit-open is not necessarily a rate limit. Track the
                # cause separately so health does not mislabel it.
                self._first_buy_provider_backoff = True
                self._first_buy_stats["circuit_open_failures"] += 1
            elif status in RATE_LIMIT_STATUSES:
                self._first_buy_provider_backoff = True
                self._first_buy_stats["rate_limit_failures"] += 1
            else:
                self._first_buy_stats["provider_failures"] += 1
            self._first_buy_stats["gecko_failures"] += 1
            log.warning(
                "first_buy_trades_failed",
                pool=normalized.pair_address,
                chain=normalized.chain_slug,
                error=redact_url(str(exc)),
            )
        finally:
            client.close()
        swaps = normalize_geckoterminal_trades(trades, chain_slug=normalized.chain_slug)
        if swaps:
            self._first_buy_stats["swaps"] += len(swaps)
        # A successful feed (even empty) is a real answer: no trades yet.
        # Only a failed request justifies the more expensive on-chain fallback.
        if not gecko_failed:
            return swaps, True
        if is_evm and self.settings.first_buy_evm_rpc_fallback_enabled:
            self._first_buy_stats["evm_rpc_fallback_hits"] += 1
            try:
                fallback = fetch_evm_pool_swaps(
                    chain_slug=normalized.chain_slug,
                    pool_address=normalized.pair_address,
                    base_address=normalized.base_address,
                    quote_address=normalized.quote_address,
                    pair_created_at=created,
                    window_minutes=self.settings.first_buy_window_minutes,
                    limit=self.settings.first_buy_swap_tx_limit,
                    price_usd=normalized.price_usd,
                )
            except Exception as exc:  # noqa: BLE001 - pair ingest must survive an RPC outage.
                log.warning(
                    "first_buy_evm_rpc_fallback_failed",
                    pool=normalized.pair_address,
                    chain=normalized.chain_slug,
                    error=redact_url(str(exc)),
                )
                return [], False
            self._first_buy_stats["evm_rpc_fallback_swaps"] += len(fallback)
            if fallback:
                log.info(
                    "first_buy_evm_rpc_fallback",
                    pool=normalized.pair_address,
                    chain=normalized.chain_slug,
                    swaps=len(fallback),
                )
            # An empty fallback cannot distinguish "no swaps" from a partial RPC
            # failure, so it stays unknown rather than fabricating zeroes.
            return fallback, bool(fallback)
        if normalized.chain_slug != "solana":
            return [], False

        # The public trades endpoint failed (often rate-limited); rebuild the
        # same wallet swaps from the pool's on-chain transactions.
        self._first_buy_stats["rpc_fallback_hits"] += 1
        rpc = SolanaRpcClient()
        try:
            fallback = fetch_pool_swaps(
                rpc,
                pool_address=normalized.pair_address,
                base_mint=normalized.base_address,
                limit=self.settings.first_buy_swap_tx_limit,
                price_usd=normalized.price_usd,
                tracked=tracked,
            )
        except Exception as exc:  # noqa: BLE001 - pair ingest must survive an RPC outage.
            log.warning(
                "first_buy_rpc_fallback_failed",
                pool=normalized.pair_address,
                error=redact_url(str(exc)),
            )
            return [], False
        finally:
            rpc.close()
        self._first_buy_stats["rpc_fallback_swaps"] += len(fallback)
        if fallback:
            log.info(
                "first_buy_rpc_fallback",
                pool=normalized.pair_address,
                swaps=len(fallback),
            )
        # An empty fallback cannot distinguish "no swaps" from a partial RPC
        # failure, so it stays unknown rather than fabricating zeroes.
        return fallback, bool(fallback)

    def _catch_up_evm_first_buys(self, session: Session) -> int:
        """Re-check EVM pairs whose first-buy window closed unobserved.

        Pairs discovered during a GeckoTerminal cooldown (or fetched while the
        provider was rate-limited) arrive after the 5-minute window has closed,
        so the normal path can no longer observe their first buys. The chain
        retains the full history, so the EVM RPC rebuilds the window from Swap
        logs. Only pairs whose stats are still unknown (buys IS NULL) and whose
        window closed within the retry horizon are retried, most-recent first.
        Returns the number of pairs whose window was observed (swaps known).
        """
        settings = self.settings
        if not settings.first_buy_catchup_enabled or not settings.first_buy_trade_fetch_enabled:
            return 0
        try:
            now = utc_now()
            window = timedelta(minutes=settings.first_buy_window_minutes)
            lookback = timedelta(hours=settings.first_buy_catchup_lookback_hours)
            retry = timedelta(hours=settings.first_buy_catchup_retry_hours)
            pairs = session.scalars(
                select(models.Pair)
                .join(models.Chain, models.Pair.chain_id == models.Chain.id)
                .outerjoin(
                    models.PairFirstWindowStats,
                    models.PairFirstWindowStats.asset_id == models.Pair.base_asset_id,
                )
                .where(models.Chain.slug.in_(("base", "ethereum")))
                .where(models.Pair.created_at_source.is_not(None))
                .where(models.Pair.created_at_source >= now - lookback)
                .where(models.Pair.created_at_source < now - window)
                .where(models.PairFirstWindowStats.buys.is_(None))
                .where(
                    or_(
                        models.PairFirstWindowStats.computed_at.is_(None),
                        models.PairFirstWindowStats.computed_at < now - retry,
                    )
                )
                .order_by(models.Pair.created_at_source.desc())
                .limit(settings.first_buy_catchup_max_pools_per_scan)
            ).all()
        except Exception as exc:  # noqa: BLE001 - catch-up must not stop the scan.
            log.warning("evm_first_buy_catchup_query_failed", error=redact_url(str(exc)))
            return 0
        if not pairs:
            return 0
        from radar.first_buys import record_first_buys  # noqa: PLC0415 - local import.

        observed = 0
        for pair in pairs:
            self._first_buy_stats["catchup_pools"] += 1
            pool = session.get(models.Pool, pair.pool_id) if pair.pool_id else None
            if pool is None or not is_evm_address(pool.address):
                continue
            base_asset = session.get(models.Asset, pair.base_asset_id)
            quote_asset = (
                session.get(models.Asset, pair.quote_asset_id) if pair.quote_asset_id else None
            )
            chain = session.get(models.Chain, pair.chain_id)
            if base_asset is None or quote_asset is None or chain is None:
                continue
            if pair.created_at_source is None:
                continue
            tracked = self._tracked_wallets(chain.slug)
            # Latest snapshot price for size_usd (best effort; None leaves it unset).
            price_usd = None
            snapshot = session.scalars(
                select(models.MarketSnapshot)
                .where(models.MarketSnapshot.pair_id == pair.id)
                .order_by(models.MarketSnapshot.ts.desc())
                .limit(1)
            ).first()
            if snapshot is not None:
                price_usd = snapshot.price_usd
            try:
                swaps = fetch_evm_pool_swaps(
                    chain_slug=chain.slug,
                    pool_address=pool.address,
                    base_address=base_asset.address,
                    quote_address=quote_asset.address,
                    pair_created_at=pair.created_at_source,
                    window_minutes=settings.first_buy_window_minutes,
                    limit=settings.first_buy_swap_tx_limit,
                    price_usd=price_usd,
                )
            except Exception as exc:  # noqa: BLE001 - one bad pair must not stop the pass.
                log.warning(
                    "evm_first_buy_catchup_failed",
                    pair=pool.address,
                    chain=chain.slug,
                    error=redact_url(str(exc)),
                )
                continue
            summary = record_first_buys(
                session,
                pair.base_asset_id,
                pair.created_at_source,
                swaps,
                tracked,
                swaps_known=bool(swaps),
            )
            for key, value in summary.items():
                self._first_buy_stats[key] = self._first_buy_stats.get(key, 0) + value
            self._first_buy_stats["catchup_swaps"] += len(swaps)
            if swaps:
                observed += 1
                self._first_buy_stats["catchup_observed"] += 1
            # Commit per pair: the RPC round-trip must not hold the write lock.
            try:
                session.commit()
            except Exception as exc:  # noqa: BLE001 - retry the pair on a later scan.
                session.rollback()
                log.warning(
                    "evm_first_buy_catchup_commit_failed",
                    pair=pool.address,
                    error=redact_url(str(exc)),
                )
        return observed

    def _record_first_buy_health(self, session: Session) -> None:
        stats = self._first_buy_stats
        skipped = stats["budget_skipped"] + stats["backoff_skipped"]
        rate_limit_failures = stats["rate_limit_failures"]
        circuit_open_failures = stats["circuit_open_failures"]
        provider_failures = stats["provider_failures"]
        if stats["gecko_failures"] == 0 and skipped == 0:
            state = "ok"
        elif provider_failures == 0 or stats["rpc_fallback_swaps"] > 0:
            # A rate-limit/cooldown or cap/backoff skip means some pools were
            # not checked, so the scan is degraded, not failed. A non-rate
            # provider failure recovers only when the fallback returned swaps.
            state = "yellow"
        else:
            state = "red"
        message = " ".join(f"{key}={value}" for key, value in stats.items())
        backoff_reasons: list[str] = []
        if rate_limit_failures:
            backoff_reasons.append("rate limited")
        if circuit_open_failures:
            backoff_reasons.append("circuit open")
        if backoff_reasons:
            reason = " and ".join(backoff_reasons)
            detail = (
                f"pacing: GeckoTerminal {reason}; first-buy fetches deferred "
                f"for the rest of this scan; {message}"
            )
            if provider_failures:
                detail = (
                    f"pacing with provider failures: GeckoTerminal {reason}; "
                    f"{provider_failures} other provider request(s) failed; "
                    f"first-buy fetches deferred for the rest of this scan; {message}"
                )
            message = detail
        record_health(
            session,
            component="source:first_buy_feed",
            state=state,
            message=message,
            error_count=provider_failures,
        )
        log.info(
            "first_buy_feed_health",
            state=state,
            rate_limited=rate_limit_failures,
            circuit_open=circuit_open_failures,
            **stats,
        )

    @staticmethod
    def _tracked_wallets(chain_slug: str) -> set[str]:
        from trading.wallets import load_wallets  # noqa: PLC0415 - avoids import cycle.

        tracked: set[str] = set()
        for entry in load_wallets():
            if str(entry.get("chain") or "solana").lower() != chain_slug:
                continue
            address = str(entry.get("address") or "")
            if not address:
                continue
            tracked.add(address if chain_slug == "solana" else address.lower())
        return tracked


def _analysis_findings(analysis: ContractAnalysis) -> list[dict[str, str]]:
    """Map a contract analysis result to a deterministic list of
    ``{flag_type, severity}`` findings.

    This list is the single source of truth for BOTH the SQL side (one
    evidence-backed ``ContractFlag`` row per finding) and the lake replay
    (which counts the archived ``contract_analysis`` payload's ``findings``
    array), so the two read paths count the same flags. Every severity used
    here is in the set the SQL path's ``_contract_flag_count`` counts
    (``warning | high | critical | black``).
    """
    findings: list[dict[str, str]] = []
    if any(reason.startswith("Contract contains") for reason in analysis.reasons):
        findings.append({"flag_type": "honeypot", "severity": "high"})
    if analysis.has_mint_function:
        findings.append({"flag_type": "mint_authority", "severity": "warning"})
    if analysis.has_pause_function:
        findings.append({"flag_type": "pause_function", "severity": "warning"})
    if analysis.ownership_renounced is False:
        findings.append({"flag_type": "ownership_not_renounced", "severity": "warning"})
    if analysis.deployer_known_rug:
        findings.append({"flag_type": "rug_deployer", "severity": "critical"})
    return findings


def backoff_sleep_seconds(iteration: int, base: int) -> int:
    return min(base, max(5, iteration * 5))


def _float_from_rpc(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
