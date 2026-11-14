"""First-buy and first-window telemetry (Phase F3)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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
    *,
    swaps_known: bool | None = None,
) -> dict[str, int]:
    """Persist first buys and the first-window buy/sell stats.

    ``swaps=[]`` with ``swaps_known`` false (or unset and empty) means the data
    was unavailable: buys, sells and ratio stay ``None`` (unknown), never zero.
    A successful empty feed passes ``swaps_known=True`` so the zeroes are
    persisted as an observed answer instead of unknown. Returns counters for
    feed telemetry.
    """
    observed = bool(swaps) if swaps_known is None else swaps_known
    from trading.copy import maybe_queue_copy  # noqa: PLC0415 - avoids import cycle.

    settings = get_settings()
    created = ensure_utc(pair_created_at)
    asset = session.get(models.Asset, asset_id)
    window = timedelta(minutes=settings.first_buy_window_minutes)
    in_window: list[dict[str, Any]] = []
    for swap in swaps:
        raw_ts = swap.get("ts")
        if raw_ts is None:
            continue
        ts = ensure_utc(raw_ts)
        if created <= ts <= created + window:
            in_window.append(swap)

    if observed:
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
    # One asset can have several pools, and each ingest only observes its own
    # pool. Keep the strongest observation instead of letting a later, quieter
    # pool overwrite richer stats. This also preserves the stats when a later
    # scan can no longer re-fetch trades after the window has closed.
    if stats is not None and _observation_rank(buys, sells) <= _observation_rank(
        stats.buys, stats.sells
    ):
        buys = stats.buys
        sells = stats.sells
        ratio = stats.buy_sell_ratio
    if stats is None:
        stats = models.PairFirstWindowStats(asset_id=asset_id)
        session.add(stats)
    stats.window_minutes = settings.first_buy_window_minutes
    stats.buys = buys
    stats.sells = sells
    stats.buy_sell_ratio = ratio
    stats.computed_at = utc_now()

    summary = {
        "tracked_buys": 0,
        "pending_copy_logs": 0,
        "paper_only_copy_logs": 0,
        "other_copy_logs": 0,
    }
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
            summary["tracked_buys"] += 1
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
            if asset is not None:
                copy_log = maybe_queue_copy(
                    session,
                    wallet,
                    asset.address,
                    _copy_size_sol(swap.get("size_sol"), settings.copy_max_size_sol),
                    now=ts,
                    use_write_queue=False,
                )
                if copy_log is not None:
                    if copy_log.status == "pending":
                        summary["pending_copy_logs"] += 1
                    elif copy_log.status == "paper_only":
                        summary["paper_only_copy_logs"] += 1
                    else:
                        summary["other_copy_logs"] += 1
    session.flush()
    return summary


def _observation_rank(buys: int | None, sells: int | None) -> tuple[int, int]:
    """Order observations by buys, then total activity; None is unknown."""
    if buys is None:
        return (-1, -1)
    return (buys, buys + (sells or 0))


def _copy_size_sol(raw_size: Any, default: Decimal) -> Decimal:
    """A positive copy size; malformed or non-positive swap sizes use the cap."""
    if raw_size is None:
        return default
    try:
        size = Decimal(str(raw_size))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return size if size.is_finite() and size > 0 else default
