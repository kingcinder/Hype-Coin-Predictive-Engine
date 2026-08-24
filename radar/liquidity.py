from __future__ import annotations

from datetime import datetime
from typing import Any

from eth_utils import keccak
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.http import HttpClient
from common.logging import get_logger
from common.time import ensure_utc
from ingestion.source_clients import get_rpc_url, rpc_pool_for
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

TRANSFER_TOPIC = "0x" + "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BURN_TOPIC = "0x" + keccak(text="Burn(address,uint256,uint256,address)").hex().removeprefix("0x")
ZERO_TOPIC = "0x" + "0" * 64
EVM_CHAINS = ("base", "ethereum")


class LiquidityRemovalWatcher:
    """Watch known EVM pools for LP burns and pair liquidity withdrawals.

    Uniswap-v2 style pools emit ``Burn`` when liquidity is removed. LP token
    ``Transfer`` events to the zero address catch an explicit LP-token burn.
    Events are keyed by chain/transaction/log so repeated scans are idempotent.
    The watcher intentionally records observation time as the decision time:
    public RPC logs do not include block timestamps in ``eth_getLogs`` and the
    risk signal must become available in the current scoring pass.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def scan(self, session: Session, *, decision_ts: datetime) -> dict[str, int]:
        decision_ts = ensure_utc(decision_ts)
        counts = {"events": 0, "lp_burns": 0, "withdrawals": 0, "errors": 0}
        if not self.settings.liquidity_removal_watcher_enabled:
            return counts
        source = session.scalar(select(models.Source).where(models.Source.name == "evm_rpc"))
        if source is None:
            return counts
        chains = session.scalars(
            select(models.Chain).where(models.Chain.slug.in_(EVM_CHAINS))
        ).all()
        for chain in chains:
            pools = session.scalars(
                select(models.Pool)
                .where(models.Pool.chain_id == chain.id)
                .order_by(models.Pool.updated_at.desc())
                .limit(self.settings.liquidity_removal_max_pools)
            ).all()
            if not pools:
                continue
            try:
                logs = self._logs_for_pools(chain.slug, [pool.address for pool in pools])
                pools_by_address = {pool.address.lower(): pool for pool in pools}
                for log_item in logs:
                    parsed = self._parse_log(log_item, pools_by_address)
                    if parsed is None:
                        continue
                    event_kind, pool, details = parsed
                    if self._persist_event(
                        session,
                        source=source,
                        pool=pool,
                        chain_slug=chain.slug,
                        event_kind=event_kind,
                        details=details,
                        decision_ts=decision_ts,
                    ):
                        counts["events"] += 1
                        if event_kind == "lp_burn":
                            counts["lp_burns"] += 1
                        else:
                            counts["withdrawals"] += 1
                record_health(
                    session,
                    component=f"source:lp_removal:{chain.slug}",
                    state="ok",
                    message=(
                        f"scanned {len(pools)} pools; "
                        f"{len(logs)} candidate logs"
                    ),
                    freshness_sec=0.0,
                )
            except Exception as exc:  # noqa: BLE001 - watcher must not stop ingestion.
                counts["errors"] += 1
                record_health(
                    session,
                    component=f"source:lp_removal:{chain.slug}",
                    state="red",
                    message=str(exc),
                    error_count=1,
                )
                log.warning("lp_removal_watch_failed", chain=chain.slug, error=str(exc))
        state = "ok" if counts["errors"] == 0 else "yellow"
        record_health(
            session,
            component="lp_removal_watcher",
            state=state,
            message=(
                f"{counts['events']} new on-chain removal events "
                f"({counts['lp_burns']} LP burns, {counts['withdrawals']} withdrawals)"
            ),
            error_count=counts["errors"],
        )
        return counts

    def _logs_for_pools(self, chain_slug: str, pool_addresses: list[str]) -> list[dict[str, Any]]:
        rpc_url = get_rpc_url(chain_slug)
        if not rpc_url:
            raise RuntimeError(f"no RPC URL for {chain_slug}")
        pool = rpc_pool_for(chain_slug)
        try:
            with HttpClient(base_url=rpc_url) as http:
                latest_payload = http.post_json(
                    "/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_blockNumber",
                        "params": [],
                    },
                )
                latest = self._hex_int(latest_payload.get("result"))
                if latest is None:
                    raise RuntimeError(f"{chain_slug}: invalid eth_blockNumber response")
                start = max(0, latest - max(1, self.settings.liquidity_removal_lookback_blocks))
                payload = http.post_json(
                    "/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "eth_getLogs",
                        "params": [
                            {
                                "fromBlock": hex(start),
                                "toBlock": hex(latest),
                                "address": pool_addresses,
                                "topics": [[BURN_TOPIC, TRANSFER_TOPIC]],
                            }
                        ],
                    },
                )
            if pool is not None:
                pool.mark_success(rpc_url)
            result = payload.get("result") if isinstance(payload, dict) else None
            return result if isinstance(result, list) else []
        except Exception:
            if pool is not None:
                pool.mark_failure(rpc_url)
            raise

    @classmethod
    def _parse_log(
        cls,
        log_item: dict[str, Any],
        pools_by_address: dict[str, models.Pool],
    ) -> tuple[str, models.Pool, dict[str, Any]] | None:
        address = str(log_item.get("address") or "").lower()
        pool = pools_by_address.get(address)
        topics = log_item.get("topics")
        if pool is None or not isinstance(topics, list) or not topics:
            return None
        signature = str(topics[0]).lower()
        if signature == BURN_TOPIC.lower():
            event_kind = "liquidity_withdrawal"
        elif (
            signature == TRANSFER_TOPIC.lower()
            and len(topics) >= 3
            and str(topics[2]).lower() == ZERO_TOPIC.lower()
            and str(topics[1]).lower() != ZERO_TOPIC.lower()
        ):
            event_kind = "lp_burn"
        else:
            return None
        tx_hash = str(log_item.get("transactionHash") or "")
        if not tx_hash:
            return None
        block_number = cls._hex_int(log_item.get("blockNumber"))
        log_index = cls._hex_int(log_item.get("logIndex"))
        if block_number is None or log_index is None:
            return None
        return event_kind, pool, {
            "tx_hash": tx_hash,
            "block_number": block_number,
            "log_index": log_index,
            "pool_address": pool.address,
            "topics": [str(topic) for topic in topics],
            "data": str(log_item.get("data") or "0x"),
            "event_signature": signature,
        }

    @staticmethod
    def _hex_int(value: Any) -> int | None:
        try:
            if isinstance(value, int):
                return value
            return int(str(value), 16)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _persist_event(
        session: Session,
        *,
        source: models.Source,
        pool: models.Pool,
        chain_slug: str,
        event_kind: str,
        details: dict[str, Any],
        decision_ts: datetime,
    ) -> bool:
        tx_hash = str(details["tx_hash"])
        log_index = int(details["log_index"])
        existing = session.scalar(
            select(models.LiquidityRemovalEvent).where(
                models.LiquidityRemovalEvent.chain_slug == chain_slug,
                models.LiquidityRemovalEvent.tx_hash == tx_hash,
                models.LiquidityRemovalEvent.log_index == log_index,
                models.LiquidityRemovalEvent.event_kind == event_kind,
            )
        )
        if existing is not None:
            return False
        asset_id = pool.base_asset_id
        session.add(
            models.LiquidityRemovalEvent(
                asset_id=asset_id,
                pool_id=pool.id,
                source_id=source.id,
                chain_slug=chain_slug,
                event_kind=event_kind,
                tx_hash=tx_hash,
                log_index=log_index,
                block_number=int(details["block_number"]),
                ts=decision_ts,
                observed_at=decision_ts,
                confidence=0.9,
                details=details,
            )
        )
        session.flush()
        return True
