from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.enums import IgnitionEventType
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from ingestion.source_clients import SolanaRpcClient, get_rpc_url
from radar.ignition import IgnitionRadar
from storage import models
from storage.repository import record_health, store_raw_evidence

log = get_logger(__name__)


class SolanaMempoolWatcher:
    """Watches a mint's recent signatures as a poor man's geyser stream.

    ``getSignaturesForAddress`` is free on public RPCs. The arrival *rate* of
    mint-associated transactions in the first minutes after pool creation is the
    sniper-bot tell: bots buy within seconds, so a burst of signatures that cannot
    be explained by organic flow is a coordination signal.
    """

    def __init__(self) -> None:
        from common.config import get_settings

        self.settings = get_settings()
        self.radar = IgnitionRadar()

    def close(self) -> None:
        return None

    def watch_asset(
        self,
        session: Session,
        *,
        asset: models.Asset,
        source: models.Source,
        decision_ts: datetime,
    ) -> dict[str, Any]:
        client = SolanaRpcClient()  # health-scored endpoint pool (blueprint §2)
        observed_at = ensure_utc(decision_ts)
        try:
            signatures = client.get_recent_signatures(
                asset.address, limit=self.settings.mempool_solana_poll_limit
            )
            seen = self._watermark(session, source_id=source.id, asset_id=asset.id)
            new_signatures = self._new_signatures(signatures, seen)
            store_raw_evidence(
                session,
                source=source,
                payload={
                    "mint": asset.address,
                    "signatures": signatures,
                    "new_count": len(new_signatures),
                },
                observed_at=observed_at,
                raw_path=f"mempool:solana:{asset.address}:{observed_at.isoformat()}",
            )
            self._set_watermark(
                session, source_id=source.id, asset_id=asset.id, signatures=signatures
            )
            burst = self._detect_burst(session, asset, signatures, observed_at)
            return {
                "signatures": len(signatures),
                "new_signatures": len(new_signatures),
                "burst": burst,
            }
        except Exception as exc:  # noqa: BLE001 - preserve exact RPC failure.
            log.warning("mempool_solana_failed", asset=asset.address, error=str(exc))
            return {"error": str(exc)}
        finally:
            client.close()

    def _detect_burst(
        self,
        session: Session,
        asset: models.Asset,
        signatures: list[dict[str, Any]],
        observed_at: datetime,
    ) -> bool:
        if len(signatures) < self.settings.mempool_burst_min_txs:
            return False
        anchor = ensure_utc(self._pool_anchor(session, asset.id) or observed_at)
        window_end = anchor + timedelta(seconds=self.settings.mempool_burst_window_seconds)
        if observed_at > window_end:
            return False
        recent = [
            signature for signature in signatures if self._signature_time(signature) is not None
        ]
        if len(recent) < self.settings.mempool_burst_min_txs:
            return False
        return self.radar.upsert_event(
            session,
            asset=asset,
            source_id=asset_id_source(session, asset) or 0,
            event_type=IgnitionEventType.SNIPER_BURST.value,
            ts=observed_at,
            observed_at=observed_at,
            confidence=0.85,
            details={
                "signatures": len(recent),
                "window_seconds": self.settings.mempool_burst_window_seconds,
                "source": "mempool:solana",
            },
        )

    def _pool_anchor(self, session: Session, asset_id: int) -> datetime | None:
        created = session.scalar(
            select(models.Pool.created_at_source)
            .join(models.Pair, models.Pair.pool_id == models.Pool.id)
            .where(models.Pair.base_asset_id == asset_id)
            .order_by(models.Pool.created_at_source)
            .limit(1)
        )
        return created

    def _watermark(self, session: Session, *, source_id: int, asset_id: int) -> set[str]:
        row = session.scalar(
            select(models.IngestionWatermark).where(
                models.IngestionWatermark.source_id == source_id,
                models.IngestionWatermark.chain_id.is_(None),
                models.IngestionWatermark.cursor_name == f"mempool:solana:{asset_id}",
            )
        )
        if not row or not row.cursor_value:
            return set()
        try:
            return set(str(row.cursor_value).split(","))
        except ValueError:
            return set()

    def _set_watermark(
        self,
        session: Session,
        *,
        source_id: int,
        asset_id: int,
        signatures: list[dict[str, Any]],
    ) -> None:
        if not signatures:
            return
        signature_ids = [str(item.get("signature") or "") for item in signatures]
        signature_ids = [item for item in signature_ids if item]
        if not signature_ids:
            return
        row = session.scalar(
            select(models.IngestionWatermark).where(
                models.IngestionWatermark.source_id == source_id,
                models.IngestionWatermark.chain_id.is_(None),
                models.IngestionWatermark.cursor_name == f"mempool:solana:{asset_id}",
            )
        )
        cursor = ",".join(signature_ids[-20:])
        if row:
            row.cursor_value = cursor
            row.updated_at = utc_now()
            return
        session.add(
            models.IngestionWatermark(
                source_id=source_id,
                cursor_name=f"mempool:solana:{asset_id}",
                cursor_value=cursor,
                observed_at=utc_now(),
            )
        )

    def _new_signatures(self, signatures: list[dict[str, Any]], seen: set[str]) -> list[str]:
        output: list[str] = []
        for item in signatures:
            signature = str(item.get("signature") or "")
            if not signature or signature in seen:
                break
            output.append(signature)
        return output

    @staticmethod
    def _signature_time(item: dict[str, Any]) -> datetime | None:
        try:
            from common.time import parse_ms_timestamp

            return parse_ms_timestamp(item.get("blockTime"))
        except (TypeError, ValueError):
            return None


def asset_id_source(session: Session, asset: models.Asset) -> int | None:
    """Best-effort source id for an asset's venue evidence (radar event source)."""
    return session.scalar(
        select(models.MarketSnapshot.source_id)
        .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
        .where(models.Pair.base_asset_id == asset.id)
        .limit(1)
    )


def run_solana_watch(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    asset_ids: list[int] | None = None,
) -> dict[str, Any]:
    from common.config import get_settings

    settings = get_settings()
    if "solana" not in settings.target_chains or not settings.mempool_enabled:
        return {"skipped": True}
    from storage.repository import get_or_create_source

    source = get_or_create_source(
        session,
        name="solana_rpc",
        source_type="chain_rpc",
        tier="chain",
        base_url=get_rpc_url("solana"),
    )
    chain = session.scalar(select(models.Chain).where(models.Chain.slug == "solana"))
    if not chain:
        return {"skipped": True}
    decision_ts = ensure_utc(decision_ts or utc_now())
    watcher = SolanaMempoolWatcher()
    stmt = select(models.Asset).where(models.Asset.chain_id == chain.id)
    if asset_ids is not None:
        stmt = stmt.where(models.Asset.id.in_(asset_ids))
    assets = session.scalars(stmt.limit(settings.solana_holder_scan_limit * 5 or 5)).all()
    watched = 0
    bursts = 0
    errors = 0
    for asset in assets:
        result = watcher.watch_asset(session, asset=asset, source=source, decision_ts=decision_ts)
        watched += 1
        if result.get("burst"):
            bursts += 1
        if "error" in result:
            errors += 1
    state = "ok" if not errors else "yellow"
    message = f"{watched} mints watched, {bursts} bursts, {errors} errors"
    record_health(
        session,
        component="source:solana_mempool",
        state=state,
        message=message,
        error_count=errors,
    )
    return {"watched": watched, "bursts": bursts, "errors": errors}
