from __future__ import annotations

from functools import partial
from time import sleep
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from catalyst.extractor import alert_upcoming_catalysts, extract_catalysts
from common.config import get_settings
from common.logging import get_logger
from common.time import floor_to_hour, utc_now
from fingerprint.engine import FingerprintEngine
from forecast.engine import run_forecast_if_due
from ingestion.birdeye_client import BirdeyeClient
from ingestion.contract_analyzer import analyze_contract
from ingestion.data_quality import check_market_snapshots
from ingestion.holder_tracker import get_evm_holders, get_solana_holders  # noqa: F401
from ingestion.normalizers import (
    NormalizedPair,
    extract_profile_links,
    normalize_dexscreener_pair,
    normalize_geckoterminal_pool,
)
from ingestion.rpc_pool import (
    POOL_CHAINS,
    get_rpc_pool,
    persist_pool_snapshots,
    record_pool_health,
)
from ingestion.source_clients import (
    DexScreenerClient,
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
        numeric_sum = sum(v for v in value.values() if isinstance(v, (int, float)))
        if numeric_sum > 0:
            return int(numeric_sum)
        # fallback: try 'count' key
        return int(value.get("count", 0))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class IngestionService:
    def __init__(self) -> None:
        self.settings = get_settings()

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

    def run_once(self, session: Session) -> dict[str, Any]:
        self.ensure_reference_data(session)
        errors: list[str] = []
        result: dict[str, Any] = {
            "pairs": 0,
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
            result["pairs"] += self._ingest_dexscreener_boosts(session)
            result["pairs"] += self._ingest_geckoterminal_new_pools(session)
            result["birdeye_tokens"] = self._ingest_birdeye_solana(session)
            result["holder_snapshots"] = self._ingest_solana_holder_snapshots(session)
            result["mempool"] = run_mempool(session, decision_ts=decision_ts)
            result["lp_removals"] = LiquidityRemovalWatcher().scan(
                session, decision_ts=decision_ts
            )
            result["prelaunch"] = len(PrelaunchQueue().scan(session, decision_ts=decision_ts))
            result["narrative"] = run_narrative(session, decision_ts=decision_ts)
            result["catalysts"] = extract_catalysts(session, decision_ts=decision_ts)
            result["catalyst_alerts"] = alert_upcoming_catalysts(
                session, decision_ts=decision_ts
            )
            radar_result = IgnitionRadar().scan(session, decision_ts=decision_ts)
            result["ignition_events"] = sum(radar_result.values())
            fingerprint = FingerprintEngine()
            result["fingerprint_clusters"] = fingerprint.learn(session)
            result["fingerprints"] = len(fingerprint.assess(session))
            result["lifecycle"] = run_lifecycle(session, decision_ts=decision_ts)
            forecast_result = run_forecast_if_due(session, decision_ts=decision_ts)
            result["forecasts"] = forecast_result.get("forecasts", 0)
            scores = score_current_assets(session, decision_ts=decision_ts)
            result["scores"] = len(scores)
            result["ntfy"] = run_notifier(session, decision_ts=decision_ts)
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
            # ── Phase 1: Contract analysis for new assets ───────────────
            try:
                contract_result = self._run_contract_analysis(session, decision_ts)
                result["contract_analysis"] = contract_result
            except Exception as ce:  # noqa: BLE001
                log.debug("contract_analysis_failed", error=str(ce))
                result["contract_analysis"] = {"error": str(ce)}
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
                ntfy_sent=(result.get("ntfy", {}).get("sent", 0)
                           if isinstance(result.get("ntfy"), dict) else 0),
                rpc_pool_notifications=result.get("rpc_pool_notifications", 0),
                rpc_pool_snapshots=result.get("rpc_pool_snapshots", 0),
                state="ok",
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - must preserve exact source failure.
            session.rollback()
            with session.begin():
                record_health(
                    session, component="worker", state="red", message=str(exc), error_count=1
                )
                record_scan_result(
                    session,
                    ts=utc_now(),
                    state="red",
                    error_message=str(exc),
                )
            errors.append(str(exc))
            log.exception("ingestion_scan_failed", error=str(exc))
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
            snap_dicts.append({
                "pair_id": s.pair_id,
                "price_usd": s.price_usd,
                "ts": s.ts,
                "source_id": s.source_id,
                "volume_usd": s.volume_usd,
            })
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
        """Analyze contracts for recently discovered assets, skipping already-analyzed ones."""
        # Find assets that don't yet have a contract_flag record
        analyzed_ids = session.scalars(
            select(models.ContractFlag.contract_id)
        ).all()
        query = select(models.Asset).order_by(desc(models.Asset.created_at)).limit(10)
        if analyzed_ids:
            query = (
                select(models.Asset)
                .where(models.Asset.id.notin_(analyzed_ids))
                .order_by(desc(models.Asset.created_at))
                .limit(10)
            )
        recent_assets = session.scalars(query).all()
        results: list[dict[str, Any]] = []
        for asset in recent_assets:
            chain_obj = self._chain(session, asset.chain_id)
            chain_name = chain_obj.slug if chain_obj else "solana"
            analysis = analyze_contract(
                asset.address,
                chain=chain_name,
            )
            results.append({
                "asset_id": asset.id,
                "address": asset.address,
                "suspicious_flags": analysis.suspicious_flags,
                "is_honeypot": analysis.is_honeypot,
                "has_mint": analysis.has_mint_function,
                "ownership_renounced": analysis.ownership_renounced,
                "reasons": analysis.reasons,
            })
        flagged = sum(1 for r in results if r["suspicious_flags"] > 0)
        return {"analyzed": len(results), "flagged": flagged}

    def _ingest_birdeye_solana(self, session: Session) -> int:
        """Ingest new tokens from Birdeye API as a third Solana data source."""
        if "solana" not in self.settings.target_chains:
            return 0
        get_or_create_source(
            session, name="birdeye", source_type="market_data", tier="venue",
            base_url=BirdeyeClient.BASE_URL,
        )
        source = self._source(session, "birdeye")
        client = BirdeyeClient()
        count = 0
        try:
            new_tokens = client.new_tokens(limit=20)
            store_raw_evidence(
                session, source=source,
                payload={"birdeye_new_tokens": new_tokens}, observed_at=utc_now(),
            )
            for token in new_tokens:
                address = token.get("address") or ""
                if not address:
                    continue
                upsert_asset(
                    session,
                    chain_id=(
                        solana_chain.id
                        if (solana_chain := self._chain(session, "solana"))
                        else 1
                    ),
                    address=address,
                    symbol=token.get("symbol") or "UNKNOWN",
                    name=token.get("name"),
                    first_seen_at=utc_now(),
                )
                count += 1
            record_health(
                session, component="source:birdeye", state="ok",
                message=f"{count} new tokens",
            )
        except Exception as exc:  # noqa: BLE001
            record_health(
                session, component="source:birdeye", state="red",
                message=str(exc), error_count=1,
            )
            log.warning("birdeye_ingestion_failed", error=str(exc))
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
                message=str(exc),
                error_count=1,
            )
            log.warning("dexscreener_profiles_failed", error=str(exc))
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
            for token in tokens[:100]:
                chain_slug = str(token.get("chainId") or "").lower()
                token_address = token.get("tokenAddress")
                if chain_slug not in self.settings.target_chains or not token_address:
                    continue
                for pair_payload in client.token_pairs(chain_slug, str(token_address)):
                    if self._store_dexscreener_pair(session, source, pair_payload):
                        count += 1
            record_health(
                session, component="source:dexscreener_pairs", state="ok", message=f"{count} pairs"
            )
        except Exception as exc:  # noqa: BLE001
            record_health(
                session,
                component="source:dexscreener_pairs",
                state="red",
                message=str(exc),
                error_count=1,
            )
            log.warning("dexscreener_pairs_failed", error=str(exc))
        finally:
            client.close()
        return count

    def _ingest_geckoterminal_new_pools(self, session: Session) -> int:
        source = self._source(session, "geckoterminal")
        client = GeckoTerminalClient()
        count = 0
        try:
            for chain_slug in self.settings.target_chains:
                if chain_slug not in ("solana", "base", "ethereum"):
                    continue
                items = client.new_pools(chain_slug)
                store_raw_evidence(
                    session,
                    source=source,
                    payload={"chain": chain_slug, "new_pools": items},
                    observed_at=utc_now(),
                )
                for pool_payload in items:
                    if self._store_geckoterminal_pool(session, source, pool_payload):
                        count += 1
            record_health(
                session,
                component="source:geckoterminal_new_pools",
                state="ok",
                message=f"{count} normalized pools",
            )
        except Exception as exc:  # noqa: BLE001
            record_health(
                session,
                component="source:geckoterminal_new_pools",
                state="red",
                message=str(exc),
                error_count=1,
            )
            log.warning("geckoterminal_new_pools_failed", error=str(exc))
        finally:
            client.close()
        return count

    def _ingest_solana_holder_snapshots(self, session: Session) -> int:
        if "solana" not in self.settings.target_chains:
            return 0
        source = self._source(session, "solana_rpc")
        chain = self._chain(session, "solana")
        if not chain:
            return 0
        assets = self._solana_holder_scan_assets(session, chain.id)
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
            for asset in assets:
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
                    raw.raw_path = raw.raw_path or f"holder_snapshot:{asset.id}:{ts.isoformat()}"
                except Exception as exc:  # noqa: BLE001 - preserve per-asset source failure.
                    errors.append(f"{asset.address}: {exc}")
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
                message=str(exc),
                error_count=1,
            )
            log.warning("solana_holders_failed", error=str(exc))
        finally:
            client.close()
        return count

    def _solana_holder_scan_assets(self, session: Session, chain_id: int) -> list[models.Asset]:
        limit = max(0, self.settings.solana_holder_scan_limit)
        if limit == 0:
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
        return True


def backoff_sleep_seconds(iteration: int, base: int) -> int:
    return min(base, max(5, iteration * 5))


def _float_from_rpc(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
