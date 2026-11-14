"""First-buy and first-window telemetry (Phase F3)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.time import ensure_utc, utc_now
from storage import models


def record_first_buys(
    session: Session,
    asset_id: int,
    pair_created_at: datetime,
    swaps: list[dict[str, Any]],
    tracked: set[str],
) -> None:
    """Persist first buys and the first-window buy/sell stats.

    ``swaps=[]`` means the data was unavailable: buys, sells and ratio stay
    ``None`` (unknown), never zero.
    """
    settings = get_settings()
    created = ensure_utc(pair_created_at)
    window = timedelta(minutes=settings.first_buy_window_minutes)
    in_window: list[dict[str, Any]] = []
    for swap in swaps:
        raw_ts = swap.get("ts")
        if raw_ts is None:
            continue
        ts = ensure_utc(raw_ts)
        if created <= ts <= created + window:
            in_window.append(swap)

    if swaps:
        buys = sum(1 for swap in in_window if swap.get("side") == "buy")
        sells = sum(1 for swap in in_window if swap.get("side") == "sell")
        ratio = Decimal(str(buys)) / Decimal(str(sells)) if sells > 0 else None
    else:
        buys = None
        sells = None
        ratio = None

    stats = session.scalar(
        select(models.PairFirstWindowStats).where(models.PairFirstWindowStats.asset_id == asset_id)
    )
    if stats is None:
        stats = models.PairFirstWindowStats(asset_id=asset_id)
        session.add(stats)
    stats.window_minutes = settings.first_buy_window_minutes
    stats.buys = buys
    stats.sells = sells
    stats.buy_sell_ratio = ratio
    stats.computed_at = utc_now()

    seen: set[tuple[str, datetime]] = set()
    for swap in in_window:
        if swap.get("side") != "buy":
            continue
        wallet = str(swap.get("wallet") or "")
        raw_ts = swap.get("ts")
        if not wallet or raw_ts is None:
            continue
        ts = ensure_utc(raw_ts)
        if (wallet, ts) in seen:
            continue
        seen.add((wallet, ts))
        existing = session.scalar(
            select(models.FirstBuy).where(
                models.FirstBuy.asset_id == asset_id,
                models.FirstBuy.wallet == wallet,
                models.FirstBuy.ts == ts,
            )
        )
        if existing is not None:
            continue
        is_tracked = wallet in tracked
        size = swap.get("size_usd")
        session.add(
            models.FirstBuy(
                asset_id=asset_id,
                wallet=wallet,
                size_usd=None if size is None else Decimal(str(size)),
                seconds_after_pair_creation=int((ts - created).total_seconds()),
                is_tracked_wallet=is_tracked,
                ts=ts,
            )
        )
        if is_tracked:
            alert = session.scalar(
                select(models.Alert).where(
                    models.Alert.asset_id == asset_id,
                    models.Alert.alert_type == "tracked_wallet_first_buy",
                    models.Alert.score_snapshot_ref == f"first_buy:{wallet}:{ts.isoformat()}",
                )
            )
            if alert is None:
                session.add(
                    models.Alert(
                        asset_id=asset_id,
                        alert_type="tracked_wallet_first_buy",
                        threshold_version="tracked_wallet",
                        score_snapshot_ref=f"first_buy:{wallet}:{ts.isoformat()}",
                        message=f"Tracked wallet {wallet} made a first buy at {ts.isoformat()}.",
                    )
                )
    session.flush()
