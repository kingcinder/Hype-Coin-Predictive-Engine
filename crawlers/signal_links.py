"""Crawler item → asset linking for cross-source fusion.

The fusion layer (``scoring/cross_source_fusion``) counts corroboration by
querying event tables — SocialMention, MarketSnapshot, IgnitionEvent,
LiquidityRemovalEvent — but the Night Crawler orchestrator only wrote raw
evidence.  That meant no crawler signal ever reached fusion, so an asset
discovered by pump_portal + dexscreener_trends + google_trends scored no
boost at all.

This module bridges the gap at write time: every item a crawler collects is
resolved to known assets (by mint/address, symbol, or token mentions in the
title/text) and upserted as a ``SocialMention`` row on the crawler's source.
Fusion's existing ``social_branch`` then sees the crawler as a corroborating
source with zero changes to the fusion query itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging import get_logger
from crawlers.sources.utils import extract_token_mentions
from storage import models
from storage.repository import stable_hash, upsert_social_mention

log = get_logger(__name__)

# Minimum symbol length for a symbol-based match — single-char symbols are
# too ambiguous to corroborate anything.
_MIN_SYMBOL_LEN = 2
# Cap the number of assets linked per item (a generic item like a global
# market snapshot could otherwise fan out to every asset on the chain).
_MAX_ASSETS_PER_ITEM = 3


@dataclass
class AssetIndex:
    """Lookup indexes over all assets — one query, no N+1 per item."""

    by_address: dict[str, list[models.Asset]] = field(default_factory=dict)
    by_symbol: dict[str, list[models.Asset]] = field(default_factory=dict)


def _coerce_ts(value: Any, fallback: datetime) -> datetime:
    """Coerce a crawler item's ``published`` value to an aware datetime.

    Most crawlers emit a datetime, but a few (e.g. farcaster) serialize
    to an ISO-8601 string; mixing types in the ``(source_id, ts, raw_ref)``
    dedup key would silently defeat the upsert.  Any unusable value falls
    back to the crawl time.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return fallback


def link_crawler_items(
    session: Session,
    *,
    source: models.Source,
    items: list[dict[str, Any]],
    observed_at: datetime,
) -> int:
    """Resolve crawler items to known assets and write SocialMention links.

    Returns the number of (item → asset) links created.  Never raises —
    linking is additive and a failure must not abort the crawl pass.
    """
    if not items:
        return 0
    try:
        index = _index_assets(session)
        linked = 0
        for item in items:
            for asset in _resolve_item_assets(item, index)[:_MAX_ASSETS_PER_ITEM]:
                try:
                    # ``ts`` is the item's own publication time (falling back
                    # to the crawl time) so the dedup key
                    # (source_id, ts, raw_ref) is stable across passes —
                    # re-crawling the same item never duplicates.
                    ts = _coerce_ts(item.get("published"), observed_at)
                    upsert_social_mention(
                        session,
                        asset_id=asset.id,
                        topic=(item.get("title") or item.get("text") or "")[:256],
                        source_id=source.id,
                        ts=ts,
                        observed_at=observed_at,
                        metrics_json=item.get("metrics") or {},
                        raw_ref=_raw_ref(item, source.id),
                    )
                    linked += 1
                except Exception:  # noqa: BLE001 - one bad item must not block the batch
                    log.debug("signal_link_failed", item=item.get("title"))
        return linked
    except Exception as exc:  # noqa: BLE001 - linking is best-effort
        log.debug("signal_linking_skipped", source=source.name, error=str(exc))
        return 0


def _index_assets(session: Session) -> AssetIndex:
    """Build lookup indexes over all assets, keyed by address and symbol.

    Symbol keys are lowercased for case-insensitive matching.
    """
    index = AssetIndex()
    for asset in session.scalars(select(models.Asset)).all():
        if asset.address:
            index.by_address.setdefault(asset.address.lower(), []).append(asset)
        symbol = (asset.symbol or "").strip().lower()
        if len(symbol) >= _MIN_SYMBOL_LEN:
            index.by_symbol.setdefault(symbol, []).append(asset)
    return index


def _resolve_item_assets(
    item: dict[str, Any],
    index: AssetIndex,
) -> list[models.Asset]:
    """Resolve a single crawler item to known assets.

    Match priority:
    1. Explicit address in metrics (``mint`` / ``token_address`` / ``address``).
    2. Explicit symbol in metrics (``symbol``), case-insensitive.
    3. Token mentions extracted from title/text (``$TICKER``, 0x, base58).
    """
    found: list[models.Asset] = []
    seen_ids: set[int] = set()
    metrics = item.get("metrics") or {}

    def _add(asset: models.Asset) -> None:
        if asset.id not in seen_ids:
            seen_ids.add(asset.id)
            found.append(asset)

    # 1. Address matches
    for key in ("mint", "token_address", "address", "pair_address"):
        raw = metrics.get(key)
        if isinstance(raw, str) and raw.strip():
            for asset in index.by_address.get(raw.strip().lower(), []):
                _add(asset)

    # 2. Symbol matches
    raw_symbol = metrics.get("symbol")
    if isinstance(raw_symbol, str):
        sym = raw_symbol.strip().lower()
        if len(sym) >= _MIN_SYMBOL_LEN:
            for asset in index.by_symbol.get(sym, []):
                _add(asset)

    # 3. Token mentions in text
    if not found:
        haystack = f"{item.get('title') or ''} {item.get('text') or ''}"
        for mention in extract_token_mentions(haystack):
            for asset in index.by_symbol.get(mention.lower(), []):
                _add(asset)

    return found


def _raw_ref(item: dict[str, Any], source_id: int) -> str:
    """Deterministic dedup key so re-crawling the same item never duplicates."""
    url = item.get("url")
    if url:
        return f"crawler:{url}"
    title = (item.get("title") or item.get("text") or "")[:128]
    return f"crawler:{source_id}:{stable_hash(title)}"
