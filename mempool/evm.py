from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.http import HttpClient
from common.logging import get_logger
from common.time import ensure_utc, floor_to_hour, utc_now
from ingestion.source_clients import get_rpc_url, rpc_pool_for
from storage import models
from storage.repository import (
    get_or_create_source,
    insert_liquidity_snapshot_once,
    insert_market_snapshot_once,
    record_health,
    store_raw_evidence,
    upsert_asset,
    upsert_contract,
    upsert_pool_and_pair,
)

log = get_logger(__name__)

PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e"
STABLE_SYMBOLS = {"USDC", "USDT", "DAI"}


class EVMFactoryWatcher:
    """Discovers new Uniswap-v2 style pools from factory PairCreated logs.

    Public RPC ``eth_getLogs`` is free and the pool exists in the log before any
    UI shows it. Each log creates point-in-time asset/contract/pool/pair rows plus
    initial snapshots, so the radar and scoring pipelines pick the token up on the
    same scan.
    """

    def __init__(self, chain_slug: str, factory_address: str) -> None:
        settings = get_settings()
        self.chain_slug = chain_slug
        self.factory_address = factory_address
        self.settings = settings
        rpc_url = get_rpc_url(chain_slug)
        if not rpc_url:
            raise ValueError(f"no RPC URL for chain {chain_slug}")
        self.http = HttpClient(base_url=rpc_url)
        self._pool = rpc_pool_for(chain_slug)
        self._pool_url = rpc_url

    def close(self) -> None:
        self.http.close()

    def watch(self, session: Session, *, source: models.Source, decision_ts: datetime) -> int:
        cursor_block = self._cursor_block(session, source_id=source.id, chain_slug=self.chain_slug)
        latest_block = self._block_number()
        if latest_block is None:
            raise RuntimeError(f"{self.chain_slug}: eth_blockNumber failed")
        if cursor_block is None:
            cursor_block = self._lookback_start(latest_block)
        if latest_block <= cursor_block:
            return 0
        logs = self._pair_created_logs(cursor_block, latest_block)
        count = 0
        for item in logs:
            parsed = self._parse_log(item)
            if not parsed:
                continue
            if self._store_pair(session, source=source, parsed=parsed, decision_ts=decision_ts):
                count += 1
        self._set_cursor_block(
            session, source_id=source.id, chain_slug=self.chain_slug, block=latest_block
        )
        return count

    # ------------------------------------------------------------------- helpers

    def _rpc(self, method: str, params: list[Any]) -> Any:
        try:
            data = self.http.post_json(
                "/",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
        except Exception:
            if self._pool is not None:
                self._pool.mark_failure(self._pool_url)
            raise
        if self._pool is not None:
            self._pool.mark_success(self._pool_url)
        return data

    def _block_number(self) -> int | None:
        data = self._rpc("eth_blockNumber", [])
        try:
            return int(str(data["result"]), 16)
        except (KeyError, TypeError, ValueError):
            return None

    def _lookback_start(self, latest_block: int) -> int:
        blocks_per_hour = 3600 // 12
        return max(0, latest_block - blocks_per_hour * self.settings.evm_lookback_hours)

    def _pair_created_logs(self, from_block: int, to_block: int) -> list[dict[str, Any]]:
        data = self._rpc(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "address": self.factory_address,
                    "topics": [PAIR_CREATED_TOPIC],
                }
            ],
        )
        try:
            result = data["result"]
            return result if isinstance(result, list) else []
        except (KeyError, TypeError):
            return []

    def _parse_log(self, item: dict[str, Any]) -> dict[str, Any] | None:
        topics = item.get("topics") or []
        if len(topics) < 4:
            return None
        return {
            "token0": self._topic_address(topics[1]),
            "token1": self._topic_address(topics[2]),
            "pair": self._topic_address(topics[3]),
            "block_number": int(str(item.get("blockNumber") or "0x0"), 16),
            "log_index": int(str(item.get("logIndex") or "0x0"), 16),
            "tx_hash": item.get("transactionHash"),
        }

    @staticmethod
    def _topic_address(topic: str) -> str:
        return "0x" + str(topic)[-40:].lower()

    def _eth_call(self, to: str, data: str) -> bytes | None:
        response = self._rpc("eth_call", [{"to": to, "data": data}, "latest"])
        try:
            result = str(response["result"])
        except (KeyError, TypeError):
            return None
        if result in ("0x", "0X") or len(result) < 66:
            return None
        return bytes.fromhex(result[2:])

    def _token_symbol(self, address: str) -> str | None:
        raw = self._eth_call(address, "0x95d89b41")
        if not raw:
            return None
        try:
            offset = int.from_bytes(raw[:32], "big")
            length = int.from_bytes(raw[offset : offset + 32], "big")
            return raw[offset + 32 : offset + 32 + length].decode("utf-8", errors="ignore")
        except (ValueError, IndexError):
            return None

    def _token_decimals(self, address: str) -> int:
        raw = self._eth_call(address, "0x313ce567")
        if not raw:
            return 18
        try:
            return int.from_bytes(raw[-32:], "big")
        except ValueError:
            return 18

    def _get_reserves(self, pair: str) -> tuple[float | None, float | None]:
        raw = self._eth_call(pair, "0x0902f1ac")
        if not raw or len(raw) < 64:
            return None, None
        reserve0 = int.from_bytes(raw[:32], "big")
        reserve1 = int.from_bytes(raw[32:64], "big")
        return float(reserve0), float(reserve1)

    def _store_pair(
        self,
        session: Session,
        *,
        source: models.Source,
        parsed: dict[str, Any],
        decision_ts: datetime,
    ) -> bool:
        chain = session.scalar(select(models.Chain).where(models.Chain.slug == self.chain_slug))
        if not chain:
            return False
        token0, token1, pair = parsed["token0"], parsed["token1"], parsed["pair"]
        if not token0 or not token1 or not pair:
            return False
        observed_at = ensure_utc(decision_ts)
        ts = floor_to_hour(observed_at)

        symbol0 = self._token_symbol(token0) or "UNKNOWN"
        symbol1 = self._token_symbol(token1) or "UNKNOWN"
        base_asset = upsert_asset(
            session,
            chain_id=chain.id,
            address=token0,
            symbol=symbol0,
            name=symbol0,
            first_seen_at=observed_at,
        )
        quote_asset = upsert_asset(
            session,
            chain_id=chain.id,
            address=token1,
            symbol=symbol1,
            name=symbol1,
            first_seen_at=observed_at,
        )
        upsert_contract(
            session,
            chain_id=chain.id,
            asset_id=base_asset.id,
            address=token0,
            observed_at=observed_at,
        )
        raw = store_raw_evidence(
            session,
            source=source,
            payload={"chain": self.chain_slug, "factory": self.factory_address, **parsed},
            observed_at=observed_at,
            raw_path=f"mempool:evm:{self.chain_slug}:{pair}",
        )
        pool, pair_row = upsert_pool_and_pair(
            session,
            chain_id=chain.id,
            dex_id="uniswap-v2",
            pair_address=pair,
            base_asset_id=base_asset.id,
            quote_asset_id=quote_asset.id,
            created_at_source=observed_at,
        )
        reserve0, reserve1 = self._get_reserves(pair)
        reserve_usd: float | None = None
        if reserve1 is not None and symbol1 in STABLE_SYMBOLS:
            decimals1 = self._token_decimals(token1)
            reserve_usd = reserve1 / (10.0**decimals1)
        insert_liquidity_snapshot_once(
            session,
            pool_id=pool.id,
            source_id=source.id,
            ts=ts,
            observed_at=observed_at,
            reserve_usd=reserve_usd,
            reserve_base=reserve0,
            reserve_quote=reserve1,
            raw_evidence_id=raw.id,
        )
        insert_market_snapshot_once(
            session,
            pair_id=pair_row.id,
            source_id=source.id,
            ts=ts,
            observed_at=observed_at,
            price_usd=None,
            volume_usd=None,
            raw_evidence_id=raw.id,
        )
        return True

    # ---------------------------------------------------------------- watermark

    def _cursor_block(self, session: Session, *, source_id: int, chain_slug: str) -> int | None:
        chain = session.scalar(select(models.Chain).where(models.Chain.slug == chain_slug))
        if not chain:
            return None
        row = session.scalar(
            select(models.IngestionWatermark).where(
                models.IngestionWatermark.source_id == source_id,
                models.IngestionWatermark.chain_id == chain.id,
                models.IngestionWatermark.cursor_name == f"mempool:evm:{chain_slug}",
            )
        )
        if not row or not row.cursor_value:
            return None
        try:
            return int(row.cursor_value)
        except ValueError:
            return None

    def _set_cursor_block(
        self, session: Session, *, source_id: int, chain_slug: str, block: int
    ) -> None:
        chain = session.scalar(select(models.Chain).where(models.Chain.slug == chain_slug))
        if not chain:
            return
        row = session.scalar(
            select(models.IngestionWatermark).where(
                models.IngestionWatermark.source_id == source_id,
                models.IngestionWatermark.chain_id == chain.id,
                models.IngestionWatermark.cursor_name == f"mempool:evm:{chain_slug}",
            )
        )
        if row:
            row.cursor_value = str(block)
            return
        session.add(
            models.IngestionWatermark(
                source_id=source_id,
                chain_id=chain.id,
                cursor_name=f"mempool:evm:{chain_slug}",
                cursor_value=str(block),
                observed_at=utc_now(),
            )
        )


def run_evm_watch(session: Session, *, decision_ts: datetime | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.mempool_enabled:
        return {"skipped": True}
    source = get_or_create_source(
        session,
        name="evm_rpc",
        source_type="chain_rpc",
        tier="chain",
        base_url=None,
    )
    decision_ts = ensure_utc(decision_ts or utc_now())
    total = 0
    errors = 0
    for chain_slug, factory in settings.evm_factories:
        if chain_slug not in settings.target_chains:
            continue
        try:
            watcher = EVMFactoryWatcher(chain_slug, factory)
            try:
                total += watcher.watch(session, source=source, decision_ts=decision_ts)
            finally:
                watcher.close()
        except Exception as exc:  # noqa: BLE001 - preserve per-chain failure.
            errors += 1
            log.warning("mempool_evm_failed", chain=chain_slug, error=str(exc))
    state = "ok" if not errors else "yellow"
    record_health(
        session,
        component="source:evm_mempool",
        state=state,
        message=f"{total} pairs discovered, {errors} chain errors",
        error_count=errors,
    )
    return {"pairs": total, "errors": errors}
