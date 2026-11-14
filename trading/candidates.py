"""Candidate feed: point-in-time rows with explicit missing data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from storage import models
from trading.price_sources import token_url

CANDIDATE_KEYS = (
    "mint",
    "chain",
    "name",
    "market_cap",
    "liquidity",
    "holders",
    "top10_holder_pct",
    "dev_wallet",
    "first_buy_wallets",
    "radar_phase",
    "risk_band",
    "missing_features",
    "forecast",
    "gmgn_url",
    "evidence_ts",
    "age_minutes",
    "tradeable",
    "tradeable_reason",
    "missing",
)


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _latest_score(session: Session, asset_id: int) -> models.Score | None:
    return session.scalar(
        select(models.Score)
        .where(models.Score.asset_id == asset_id)
        .order_by(models.Score.decision_ts.desc())
        .limit(1)
    )


def _latest_snapshot(session: Session, asset_id: int):
    return session.execute(
        select(models.MarketSnapshot.ts, models.MarketSnapshot.price_usd)
        .join(models.Pair, models.Pair.id == models.MarketSnapshot.pair_id)
        .where(models.Pair.base_asset_id == asset_id)
        .order_by(models.MarketSnapshot.ts.desc())
        .limit(1)
    ).first()


def _latest_liquidity(session: Session, asset_id: int) -> Decimal | None:
    row = session.scalar(
        select(models.LiquiditySnapshot.reserve_quote)
        .join(models.Pool, models.Pool.id == models.LiquiditySnapshot.pool_id)
        .join(models.Pair, models.Pair.pool_id == models.Pool.id)
        .where(models.Pair.base_asset_id == asset_id)
        .order_by(models.LiquiditySnapshot.ts.desc())
        .limit(1)
    )
    return None if row is None else Decimal(str(row))


def _latest_phase(session: Session, asset_id: int) -> str | None:
    return session.scalar(
        select(models.LifecycleEvent.phase)
        .where(models.LifecycleEvent.asset_id == asset_id)
        .order_by(models.LifecycleEvent.ts.desc())
        .limit(1)
    )


def _latest_forecast(session: Session, asset_id: int) -> models.Forecast | None:
    return session.scalar(
        select(models.Forecast)
        .where(models.Forecast.asset_id == asset_id)
        .order_by(models.Forecast.decision_ts.desc())
        .limit(1)
    )


def _chain_slug(session: Session, chain_id: int) -> str:
    return session.scalar(select(models.Chain.slug).where(models.Chain.id == chain_id)) or ""


def _holder_rows(session: Session, asset_id: int):
    latest_ts = session.scalar(
        select(func.max(models.Holder.ts)).where(models.Holder.asset_id == asset_id)
    )
    if latest_ts is None:
        return None, []
    rows = list(
        session.scalars(
            select(models.Holder).where(
                models.Holder.asset_id == asset_id,
                models.Holder.ts == latest_ts,
            )
        ).all()
    )
    return latest_ts, rows


def _top10_holder_pct(session: Session, asset_id: int) -> float | None:
    _, rows = _holder_rows(session, asset_id)
    if not rows:
        return None
    pcts = [row.pct_supply for row in rows if row.pct_supply is not None]
    if not pcts:
        return None
    return float(sum(sorted(pcts, reverse=True)[:10]))


def _holders_count(session: Session, asset_id: int) -> int | None:
    _, rows = _holder_rows(session, asset_id)
    if not rows:
        return None
    return len(rows)


def _dev_wallet(session: Session, asset_id: int) -> str | None:
    wallet = session.scalar(
        select(models.Contract.deployer_wallet)
        .where(models.Contract.asset_id == asset_id)
        .order_by(models.Contract.created_at.desc())
        .limit(1)
    )
    return wallet if wallet else None


def _missing_features(session: Session, score: models.Score | None) -> Any:
    if score is None:
        return None
    exp = session.scalar(
        select(models.ScoreExplanation).where(models.ScoreExplanation.score_id == score.id)
    )
    if exp is None:
        return None
    return list(exp.missing_features or [])


def _build(
    session: Session,
    asset: models.Asset,
    *,
    now: datetime,
    price_source,
    settings,
) -> dict | None:
    score = _latest_score(session, asset.id)
    snapshot = _latest_snapshot(session, asset.id)
    score_ts = _aware(score.decision_ts) if score is not None else None
    snapshot_ts = _aware(snapshot[0]) if snapshot is not None else None
    stamps = [ts for ts in (score_ts, snapshot_ts) if ts is not None]
    if not stamps:
        return None
    evidence_ts = max(stamps)
    chain = _chain_slug(session, asset.chain_id)

    missing: list[str] = []

    market_cap = None
    missing.append("market_cap")

    liquidity = _latest_liquidity(session, asset.id)
    if liquidity is None:
        missing.append("liquidity")

    holders = _holders_count(session, asset.id)
    if holders is None:
        missing.append("holders")

    top10 = _top10_holder_pct(session, asset.id)
    if top10 is None:
        missing.append("top10_holder_pct")

    dev_wallet = _dev_wallet(session, asset.id)
    if dev_wallet is None:
        missing.append("dev_wallet")

    # No persisted source for first-buy wallets in this codebase.
    first_buy_wallets = None
    missing.append("first_buy_wallets")

    radar_phase = _latest_phase(session, asset.id)
    if radar_phase is None:
        missing.append("radar_phase")

    risk_band = score.risk_band if score is not None else None
    if risk_band is None:
        missing.append("risk_band")

    mf = _missing_features(session, score)
    if mf is None:
        missing.append("missing_features")
    missing_features = mf

    forecast_row = _latest_forecast(session, asset.id)
    insufficient = True
    value = None
    if forecast_row is not None:
        value = forecast_row.p_ignition_24h
        details = forecast_row.details or {}
        insufficient = details.get("status") == "insufficient_data" or value is None
    if insufficient:
        missing.append("forecast")
    forecast = {
        "value": None if insufficient else (float(value) if value is not None else None),
        "insufficient_data": bool(insufficient),
    }

    gmgn_url = None
    try:
        if chain in ("solana", "base", "ethereum"):
            gmgn_url = token_url(chain, asset.address)
    except ValueError:
        gmgn_url = None
    if gmgn_url is None:
        missing.append("gmgn_url")

    quote = price_source.price(asset.address, chain)
    tradeable = quote is not None

    missing = sorted(set(missing))

    return {
        "mint": asset.address,
        "chain": chain,
        "name": asset.name or asset.symbol,
        "market_cap": market_cap,
        "liquidity": liquidity,
        "holders": holders,
        "top10_holder_pct": top10,
        "dev_wallet": dev_wallet,
        "first_buy_wallets": first_buy_wallets,
        "radar_phase": radar_phase,
        "risk_band": risk_band,
        "missing_features": missing_features,
        "forecast": forecast,
        "gmgn_url": gmgn_url,
        "evidence_ts": evidence_ts,
        "age_minutes": int((now - (_aware(asset.first_seen_at) or now)).total_seconds() // 60),
        "tradeable": tradeable,
        "tradeable_reason": None if tradeable else "no_live_price",
        "missing": missing,
    }


def build_candidates(session: Session, *, n: int = 10, now: datetime, price_source) -> dict:
    settings = get_settings()
    n = max(1, min(int(n), 50))
    cutoff = now - timedelta(hours=settings.candidate_max_age_hours)
    assets = session.scalars(
        select(models.Asset).where(
            models.Asset.first_seen_at >= cutoff,
            models.Asset.first_seen_at <= now,
        )
    ).all()

    rows: list[tuple[float, Decimal, dict]] = []
    excluded = 0
    for asset in assets:
        candidate = _build(session, asset, now=now, price_source=price_source, settings=settings)
        if candidate is None:
            excluded += 1
            continue
        score = _latest_score(session, asset.id)
        risk = float(score.risk) if score is not None else float("inf")
        liquidity = candidate["liquidity"] if candidate["liquidity"] is not None else Decimal("0")
        if not isinstance(liquidity, Decimal):
            liquidity = Decimal(str(liquidity))
        rows.append((risk, liquidity, candidate))

    rows.sort(key=lambda item: (item[0], -item[1]))
    return {
        "candidates": [candidate for _, _, candidate in rows[:n]],
        "excluded_no_evidence": excluded,
        "generated_at": now.isoformat(),
    }


def get_candidate(session: Session, mint: str, *, now: datetime, price_source) -> dict | None:
    settings = get_settings()
    asset = session.scalar(select(models.Asset).where(models.Asset.address == mint))
    if asset is None:
        return None
    return _build(session, asset, now=now, price_source=price_source, settings=settings)
