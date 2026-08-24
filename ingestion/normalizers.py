from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from common.time import parse_ms_timestamp


@dataclass(frozen=True)
class NormalizedPair:
    chain_slug: str
    dex_id: str
    pair_address: str
    base_address: str
    base_symbol: str
    base_name: str | None
    quote_address: str | None
    quote_symbol: str | None
    quote_name: str | None
    pair_created_at: datetime | None
    price_usd: float | None
    volume_usd: float | None
    liquidity_usd: float | None
    reserve_base: float | None
    reserve_quote: float | None
    buys: int | None
    sells: int | None
    trades: int | None
    website_url: str | None = None
    github_url: str | None = None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError:
        return None


def _link_of_type(info: dict[str, Any], *names: str) -> str | None:
    links = info.get("links") or []
    for link in links:
        url = str(link.get("url") or "")
        label = str(link.get("label") or link.get("type") or "").lower()
        if any(name in label or name in url.lower() for name in names):
            return url
    websites = info.get("websites") or []
    for item in websites:
        url = str(item.get("url") or "")
        if url:
            return url
    return None


def normalize_dexscreener_pair(payload: dict[str, Any]) -> NormalizedPair | None:
    base = payload.get("baseToken") or {}
    quote = payload.get("quoteToken") or {}
    pair_address = payload.get("pairAddress")
    chain_slug = payload.get("chainId")
    base_address = base.get("address")
    if not chain_slug or not pair_address or not base_address:
        return None

    txns = payload.get("txns") or {}
    txn_window = txns.get("h1") or txns.get("h24") or txns.get("m5") or {}
    buys = _int(txn_window.get("buys"))
    sells = _int(txn_window.get("sells"))
    liquidity = payload.get("liquidity") or {}
    volume = payload.get("volume") or {}
    info = payload.get("info") or {}

    return NormalizedPair(
        chain_slug=str(chain_slug).lower(),
        dex_id=str(payload.get("dexId") or "unknown_dex"),
        pair_address=str(pair_address),
        base_address=str(base_address),
        base_symbol=str(base.get("symbol") or "UNKNOWN"),
        base_name=base.get("name"),
        quote_address=quote.get("address"),
        quote_symbol=quote.get("symbol"),
        quote_name=quote.get("name"),
        pair_created_at=parse_ms_timestamp(payload.get("pairCreatedAt")),
        price_usd=_float(payload.get("priceUsd")),
        volume_usd=_float(volume.get("h1") or volume.get("h24") or volume.get("m5")),
        liquidity_usd=_float(liquidity.get("usd")),
        reserve_base=_float(liquidity.get("base")),
        reserve_quote=_float(liquidity.get("quote")),
        buys=buys,
        sells=sells,
        trades=(buys or 0) + (sells or 0) if buys is not None or sells is not None else None,
        website_url=_link_of_type(info, "website", "site"),
        github_url=_link_of_type(info, "github"),
    )


def _relationship_address(payload: dict[str, Any], relationship_name: str) -> str | None:
    relationship = (payload.get("relationships") or {}).get(relationship_name) or {}
    data = relationship.get("data") or {}
    token_id = data.get("id")
    if not token_id:
        return None
    parts = str(token_id).split("_", 1)
    return parts[1] if len(parts) == 2 else str(token_id)


def _relationship_chain(payload: dict[str, Any]) -> str | None:
    pool_id = payload.get("id")
    if not pool_id:
        return None
    network = str(pool_id).split("_", 1)[0].lower()
    return "ethereum" if network == "eth" else network


def _relationship_dex(payload: dict[str, Any]) -> str:
    relationship = (payload.get("relationships") or {}).get("dex") or {}
    data = relationship.get("data") or {}
    return str(data.get("id") or "unknown_dex")


def _split_pool_name(value: Any) -> tuple[str, str | None]:
    parts = [part.strip() for part in str(value or "").split("/", 1)]
    base = parts[0] if parts and parts[0] else "UNKNOWN"
    quote = parts[1] if len(parts) > 1 and parts[1] else None
    return base[:64], quote[:64] if quote else None


def normalize_geckoterminal_pool(payload: dict[str, Any]) -> NormalizedPair | None:
    attributes = payload.get("attributes") or {}
    pair_address = attributes.get("address")
    base_address = _relationship_address(payload, "base_token")
    if not pair_address or not base_address:
        return None

    base_symbol, quote_symbol = _split_pool_name(attributes.get("name"))
    txns = attributes.get("transactions") or {}
    txn_window = txns.get("h1") or txns.get("h24") or txns.get("m5") or {}
    buys = _int(txn_window.get("buys"))
    sells = _int(txn_window.get("sells"))
    volume = attributes.get("volume_usd") or {}

    return NormalizedPair(
        chain_slug=_relationship_chain(payload) or "unknown",
        dex_id=_relationship_dex(payload),
        pair_address=str(pair_address),
        base_address=str(base_address),
        base_symbol=base_symbol,
        base_name=None,
        quote_address=_relationship_address(payload, "quote_token"),
        quote_symbol=quote_symbol,
        quote_name=None,
        pair_created_at=_parse_iso_timestamp(attributes.get("pool_created_at")),
        price_usd=_float(attributes.get("base_token_price_usd")),
        volume_usd=_float(volume.get("h1") or volume.get("h24") or volume.get("m5")),
        liquidity_usd=_float(attributes.get("reserve_in_usd")),
        reserve_base=None,
        reserve_quote=None,
        buys=buys,
        sells=sells,
        trades=(buys or 0) + (sells or 0) if buys is not None or sells is not None else None,
    )


def extract_profile_links(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    website_url: str | None = None
    github_url: str | None = None
    for link in payload.get("links") or []:
        url = str(link.get("url") or "")
        label = str(link.get("label") or link.get("type") or "").lower()
        if not website_url and ("website" in label or "site" in label):
            website_url = url
        if not github_url and ("github" in label or "github.com" in url.lower()):
            github_url = url
    return website_url, github_url
