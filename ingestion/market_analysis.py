from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value)


def _age_seconds(value: Any, now: datetime) -> float | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0.0, (now - value.astimezone(UTC)).total_seconds())


def _row_value(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def select_dexscreener_pair(
    pairs: list[dict[str, Any]], address: str, chain: str | None = None
) -> dict[str, Any] | None:
    target = address.casefold()
    chain_key = chain.casefold() if chain else None
    matches = [
        pair
        for pair in pairs
        if str((pair.get("baseToken") or {}).get("address") or "").casefold() == target
        and (chain_key is None or str(pair.get("chainId") or "").casefold() == chain_key)
    ]
    return max(
        matches,
        key=lambda pair: _number((pair.get("liquidity") or {}).get("usd")) or 0.0,
        default=None,
    )


def build_market_evidence(
    live_pair: dict[str, Any] | None,
    stored_rows: list[Any],
    *,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    now = fetched_at or datetime.now(UTC)
    fetched = _iso(now)
    live: dict[str, Any] | None = None
    if live_pair is not None:
        base = live_pair.get("baseToken") or {}
        liquidity = live_pair.get("liquidity") or {}
        live = {
            "provider": "DexScreener",
            "chain": live_pair.get("chainId"),
            "dex": live_pair.get("dexId"),
            "pair_address": live_pair.get("pairAddress"),
            "token_address": base.get("address"),
            "symbol": base.get("symbol"),
            "name": base.get("name"),
            "price_usd": _number(live_pair.get("priceUsd")),
            "liquidity_usd": _number(liquidity.get("usd")),
            "volume_usd": {
                str(window): _number(amount)
                for window, amount in (live_pair.get("volume") or {}).items()
            },
            "txns": live_pair.get("txns") or {},
            "price_change_pct": live_pair.get("priceChange") or {},
            "market_cap_usd": _number(live_pair.get("marketCap")),
            "fdv_usd": _number(live_pair.get("fdv")),
            "pair_created_at_ms": live_pair.get("pairCreatedAt"),
            "observed_at": fetched,
            "url": live_pair.get("url"),
        }

    history: list[dict[str, Any]] = []
    for row in stored_rows:
        observed_at = _row_value(row, "observed_at")
        age_seconds = _age_seconds(observed_at, now)
        history.append(
            {
                "ts": _iso(_row_value(row, "ts")),
                "observed_at": _iso(observed_at),
                "age_seconds": age_seconds,
                "freshness": (
                    "unknown"
                    if age_seconds is None
                    else "stale"
                    if age_seconds > 6 * 60 * 60
                    else "recent"
                ),
                "price_usd": _number(_row_value(row, "price_usd")),
                "volume_usd": _number(_row_value(row, "volume_usd")),
                "trades": _row_value(row, "trades"),
                "buys": _row_value(row, "buys"),
                "sells": _row_value(row, "sells"),
                "liquidity_usd": _number(_row_value(row, "liquidity_usd")),
                "source": _row_value(row, "source"),
            }
        )
    return {
        "provider": "DexScreener",
        "fetched_at": fetched,
        "history_status": "available" if history else "no_stored_history",
        "live_status": "fetched" if live else "unavailable",
        "live": live,
        "history": history,
    }
