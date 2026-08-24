"""DuckDB-backed feature read path over the Parquet lake.

The SQL path computes the market/liquidity feature block by hammering the
``market_snapshots`` / ``liquidity_snapshots`` / ``pairs`` tables for every
asset every scan. ``LakeFeatureFactory`` replaces those reads: DuckDB queries
the archived Parquet evidence directly, reconstructs the normalized market and
liquidity series with the same extraction rules as ``ingestion.normalizers``
(the GeckoTerminal ``new_pools`` payload shape, the same ``h1|h24|m5`` window
precedence, the same hourly floor and first-wins dedup), and then feeds the
shared ``compute_market_block`` math — the *identical* formulas the SQL path
uses. A parity test (`tests/test_lake_features.py`) seeds both paths from the
same underlying numbers and asserts the feature values match feature-for-feature.

Beyond the market/liquidity block, the lake path reconstructs the on-chain
holder and contract-flag features from the archived RPC evidence payloads:

- ``holder_count`` / ``holder_growth`` / ``top_holder_concentration`` come
  from ``chain_rpc`` holder-snapshot evidence (the ``solana_rpc``
  ``largest_accounts`` + ``supply`` payloads that the SQL path turns into
  ``Holder`` rows), deduped per (hour, wallet) exactly like
  ``insert_holder_once``.
- ``suspicious_contract_flags`` counts the evidence-backed ``low_liquidity``
  contract flags — the GeckoTerminal pool scans whose
  ``reserve_in_usd < min_discovery_liquidity_usd`` created a ``ContractFlag``
  row in the SQL path.

The store surface is the same ``ArchiveStore`` the compactor writes, so this
works against the local disk lake (zero-container profile) and MinIO/S3
(docker profile) with no extra dependencies.
"""

from __future__ import annotations

import json
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc, floor_to_hour
from features.factory import FeatureValue, _feature, compute_market_block
from ops.archive import ArchiveStore, make_store
from storage import models
from storage.repository import upsert_feature

log = get_logger(__name__)

# The feature names the lake path can serve from the archived evidence: the
# market/liquidity block plus the on-chain holder and contract-flag features.
# Everything else in the full feature set needs SQL-side state (narratives,
# forecasts, lifecycle) and is out of scope for a lake read.
LAKE_FEATURE_NAMES: tuple[str, ...] = (
    "five_min_return",
    "one_hour_return",
    "volume_acceleration",
    "liquidity_depth",
    "liquidity_change",
    "buy_sell_ratio",
    "unique_buyers_estimate",
    "pair_age_minutes",
    "spread_estimate",
    "volatility",
    "venue_agreement",
    "holder_count",
    "holder_growth",
    "top_holder_concentration",
    "suspicious_contract_flags",
)

_RECONSTRUCT_SQL = """
WITH raw AS (
    SELECT observed_at, source_id, payload_json
    FROM read_parquet($files, union_by_name = true)
    WHERE source_type = 'market_data'
      AND observed_at <= $decision_ts
      AND json_extract_string(payload_json, '$.new_pools') IS NOT NULL
),
pools AS (
    SELECT
        r.observed_at AS observed_at,
        r.source_id AS source_id,
        t.pool AS pool
    FROM raw AS r
    CROSS JOIN UNNEST(
        from_json(json_extract_string(r.payload_json, '$.new_pools'), '["JSON"]')
    ) AS t(pool)
),
norm AS (
    SELECT
        observed_at,
        source_id,
        date_trunc('hour', observed_at) AS ts,
        split_part(
            json_extract_string(pool, '$.relationships.base_token.data.id'), '_', 2
        ) AS base_address,
        json_extract_string(pool, '$.attributes.address') AS pair_address,
        json_extract_string(pool, '$.attributes.pool_created_at') AS pool_created_at,
        TRY_CAST(json_extract_string(pool, '$.attributes.base_token_price_usd') AS DOUBLE)
            AS price_usd,
        COALESCE(
            NULLIF(
                TRY_CAST(
                    json_extract_string(pool, '$.attributes.volume_usd.h1') AS DOUBLE
                ),
                0.0
            ),
            NULLIF(
                TRY_CAST(
                    json_extract_string(pool, '$.attributes.volume_usd.h24') AS DOUBLE
                ),
                0.0
            ),
            NULLIF(
                TRY_CAST(
                    json_extract_string(pool, '$.attributes.volume_usd.m5') AS DOUBLE
                ),
                0.0
            )
        ) AS volume_usd,
        COALESCE(
            NULLIF(
                TRY_CAST(
                    json_extract_string(pool, '$.attributes.transactions.h1.buys') AS BIGINT
                ),
                0
            ),
            NULLIF(
                TRY_CAST(
                    json_extract_string(pool, '$.attributes.transactions.h24.buys') AS BIGINT
                ),
                0
            ),
            NULLIF(
                TRY_CAST(
                    json_extract_string(pool, '$.attributes.transactions.m5.buys') AS BIGINT
                ),
                0
            )
        ) AS buys,
        COALESCE(
            NULLIF(
                TRY_CAST(
                    json_extract_string(pool, '$.attributes.transactions.h1.sells') AS BIGINT
                ),
                0
            ),
            NULLIF(
                TRY_CAST(
                    json_extract_string(pool, '$.attributes.transactions.h24.sells') AS BIGINT
                ),
                0
            ),
            NULLIF(
                TRY_CAST(
                    json_extract_string(pool, '$.attributes.transactions.m5.sells') AS BIGINT
                ),
                0
            )
        ) AS sells,
        TRY_CAST(json_extract_string(pool, '$.attributes.reserve_in_usd') AS DOUBLE)
            AS reserve_usd
    FROM pools
    WHERE base_address = $asset_address
      AND base_address IS NOT NULL
      AND pair_address IS NOT NULL
),
-- First observation wins per (pair, hour): the same first-wins dedup the SQL
-- path's insert_market_snapshot_once applies per (pair, ts, source).
deduped AS (
    SELECT *
    FROM (
        SELECT
            *,
            row_number() OVER (
                PARTITION BY pair_address, ts ORDER BY observed_at
            ) AS rn
        FROM norm
    )
    WHERE rn = 1
)
SELECT observed_at, source_id, ts, pair_address, pool_created_at,
       price_usd, volume_usd, buys, sells, reserve_usd
FROM deduped
ORDER BY ts
"""


_HOLDER_RECONSTRUCT_SQL = """
WITH raw AS (
    SELECT observed_at, payload_json
    FROM read_parquet($files, union_by_name = true)
    WHERE source_type = 'chain_rpc'
      AND observed_at <= $decision_ts
      AND json_extract_string(payload_json, '$.mint') = $asset_address
      AND json_extract_string(payload_json, '$.largest_accounts') IS NOT NULL
)
SELECT
    observed_at,
    date_trunc('hour', observed_at) AS ts,
    TRY_CAST(json_extract_string(payload_json, '$.supply') AS DOUBLE) AS supply,
    json_extract_string(payload_json, '$.largest_accounts') AS accounts_json
FROM raw
ORDER BY observed_at
"""

_FLAG_RECONSTRUCT_SQL = """
WITH raw AS (
    SELECT observed_at, payload_json
    FROM read_parquet($files, union_by_name = true)
    WHERE source_type = 'market_data'
      AND observed_at <= $decision_ts
      AND json_extract_string(payload_json, '$.new_pools') IS NOT NULL
),
pools AS (
    SELECT
        r.observed_at AS observed_at,
        t.pool AS pool
    FROM raw AS r
    CROSS JOIN UNNEST(
        from_json(json_extract_string(r.payload_json, '$.new_pools'), '["JSON"]')
    ) AS t(pool)
)
SELECT
    observed_at,
    split_part(
        json_extract_string(pool, '$.relationships.base_token.data.id'), '_', 2
    ) AS base_address,
    TRY_CAST(json_extract_string(pool, '$.attributes.reserve_in_usd') AS DOUBLE)
        AS reserve_usd
FROM pools
WHERE base_address = $asset_address
  AND base_address IS NOT NULL
"""


@dataclass(frozen=True)
class LakeMarketSeries:
    market_rows: list[SimpleNamespace]
    liquidity_rows: list[SimpleNamespace]
    pair_created_at: datetime | None


@dataclass(frozen=True)
class LakeReconstruction:
    """Everything the lake read path reconstructs for one asset."""

    market_rows: list[SimpleNamespace]
    liquidity_rows: list[SimpleNamespace]
    pair_created_at: datetime | None
    holder_count: float | None
    holder_growth: float | None
    holder_concentration: float | None
    suspicious_contract_flags: float


class LakeFeatureFactory:
    """Computes the lake-covered feature block from the Parquet lake: the
    market/liquidity series plus the on-chain holder and contract-flag
    features.

    ``build_for_asset`` returns the same ``FeatureValue`` set the SQL path
    produces for those names (identical math via ``compute_market_block``,
    identical holder/flag reconstruction rules), so the two read paths are
    interchangeable for the lake-covered features.

    Reconstructions are cached at the class level, keyed by
    ``(store, asset address, hour)``: repeated backtest steps over the same
    window create a fresh ``LakeFeatureFactory`` every hour, and a per-instance
    cache would be useless. The cache is a bounded LRU, is exact when the
    decision lands on an hour boundary (the backtest contract — ``hours_between``
    steps on the hour; ``build_and_persist_features`` replays those exact
    times), and is bypassed entirely for sub-hour decision times so they never
    reuse a stale within-hour snapshot. The archived lake is treated as
    immutable within the process (the replay contract) — call
    :meth:`clear_cache` if new evidence is compacted into the lake mid-process.
    """

    # Class-level ``(store, asset_address, hour) -> LakeReconstruction``
    # cache, shared across factory instances, bounded via LRU eviction.
    _cache: OrderedDict[tuple[object, str, datetime], LakeReconstruction] = OrderedDict()
    _cache_lock = threading.RLock()
    cache_max_entries: int = 4096

    @classmethod
    def clear_cache(cls) -> None:
        """Drop all cached reconstructions (e.g. after new evidence is
        compacted into the lake)."""
        with cls._cache_lock:
            cls._cache.clear()

    def __init__(
        self,
        store: ArchiveStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or make_store(self.settings)

    def _store_key(self) -> tuple[str, ...]:
        """Stable identity for the archive store this factory reads, so the
        ``(asset, hour)`` cache never mixes lakes with different backends,
        roots, or prefixes."""
        settings = self.settings
        if settings.archive_backend_is_local:
            return ("local", settings.archive_local_dir, settings.archive_prefix)
        return ("s3", settings.minio_endpoint, settings.minio_bucket, settings.archive_prefix)

    def build_for_asset(
        self, *, asset_address: str, decision_ts: datetime
    ) -> dict[str, FeatureValue]:
        """Reconstruct the lake-covered features for one asset: the
        market/liquidity block plus the on-chain holder and contract-flag
        features, with the same missing semantics the SQL path reports."""
        recon = self._reconstruct(asset_address, decision_ts)
        asset = SimpleNamespace(
            first_seen_at=recon.pair_created_at or ensure_utc(decision_ts)
        )
        pairs = (
            [SimpleNamespace(created_at_source=recon.pair_created_at)]
            if recon.pair_created_at is not None
            else []
        )
        values = compute_market_block(
            asset=asset,
            pairs=pairs,
            market_rows=recon.market_rows,
            liquidity_rows=recon.liquidity_rows,
            decision_ts=decision_ts,
        )
        holder_src = 1 if recon.holder_count is not None else 0
        holder_fresh = 1.0 if recon.holder_count is not None else 0.0
        values["holder_count"] = _feature(
            "holder_count", recon.holder_count, holder_src, holder_fresh
        )
        values["holder_growth"] = _feature(
            "holder_growth",
            recon.holder_growth,
            1 if recon.holder_growth is not None else 0,
            1.0 if recon.holder_growth is not None else 0.0,
        )
        values["top_holder_concentration"] = _feature(
            "top_holder_concentration",
            recon.holder_concentration,
            1 if recon.holder_concentration is not None else 0,
            1.0 if recon.holder_concentration is not None else 0.0,
        )
        # The SQL path reports suspicious_contract_flags as 0.0 (never
        # missing) even with no flags; the lake path mirrors that.
        values["suspicious_contract_flags"] = _feature(
            "suspicious_contract_flags", recon.suspicious_contract_flags, 1, 1.0
        )
        return values

    def persist_for_assets(
        self,
        session: Session,
        *,
        decision_ts: datetime,
        asset_ids: list[int] | None = None,
    ) -> dict[int, dict[str, FeatureValue]]:
        """Compute the lake-covered block (market/liquidity + holder +
        contract flags) for each asset from the archived Parquet lake and
        persist it as ``Feature`` rows.

        This is the ``feature_source="lake"`` replay path: it reads DuckDB
        over the archived evidence and writes feature rows exactly like the
        SQL path (same ``upsert_feature``), without touching any live
        normalized tables.
        """
        stmt = select(models.Asset)
        if asset_ids is not None:
            stmt = stmt.where(models.Asset.id.in_(asset_ids))
        assets = session.scalars(stmt).all()
        output: dict[int, dict[str, FeatureValue]] = {}
        for asset in assets:
            values = self.build_for_asset(
                asset_address=asset.address, decision_ts=decision_ts
            )
            output[asset.id] = values
            for value in values.values():
                upsert_feature(
                    session,
                    asset_id=asset.id,
                    decision_ts=decision_ts,
                    feature_name=value.name,
                    feature_value=value.value,
                    source_count=value.source_count,
                    freshness_score=value.freshness_score,
                    missing_flag=value.missing,
                    source_refs=value.source_refs,
                )
        return output

    def _series_from_lake(
        self, asset_address: str, decision_ts: datetime
    ) -> LakeMarketSeries:
        """Reconstruct the normalized market/liquidity series via DuckDB."""
        recon = self._reconstruct(asset_address, decision_ts)
        return LakeMarketSeries(
            recon.market_rows, recon.liquidity_rows, recon.pair_created_at
        )

    def _reconstruct(
        self, asset_address: str, decision_ts: datetime
    ) -> LakeReconstruction:
        """Reconstruct for an asset at a decision time, serving the
        ``(asset, hour)`` cache when the decision lands on an hour boundary.

        Sub-hour decision times always compute fresh: a within-hour snapshot
        depends on exactly which evidence arrived before ``decision_ts``, so
        only hour-boundary decisions (the backtest's step granularity) are
        safe to cache.
        """
        decision_ts = ensure_utc(decision_ts)
        hour = floor_to_hour(decision_ts)
        if decision_ts != hour:
            return self._reconstruct_uncached(asset_address, decision_ts)
        key = (self._store_key(), asset_address, hour)
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        recon = self._reconstruct_uncached(asset_address, decision_ts)
        with self._cache_lock:
            if len(self._cache) >= self.cache_max_entries:
                self._cache.popitem(last=False)
            self._cache[key] = recon
        return recon

    def _reconstruct_uncached(
        self, asset_address: str, decision_ts: datetime
    ) -> LakeReconstruction:
        """Run all lake reconstruction queries in one DuckDB session over the
        archived evidence files."""
        keys = self.store.list_objects(self.settings.archive_prefix)
        if not keys:
            return LakeReconstruction([], [], None, None, None, None, 0.0)
        import duckdb

        decision_ts = ensure_utc(decision_ts)
        # The compactor writes lake timestamps as naive UTC (TIMESTAMP), so
        # DuckDB truncation/comparison needs no tz machinery. Bind a naive-UTC
        # decision time to match the column type.
        naive_ts = decision_ts.replace(tzinfo=None)
        with tempfile.TemporaryDirectory(prefix="serpent_lake_features_") as tmp:
            files: list[str] = []
            for index, key in enumerate(sorted(keys)):
                dest = Path(tmp) / f"{index:04d}.parquet"
                self.store.download_to(key, dest)
                files.append(str(dest))
            con = duckdb.connect()
            try:
                market_rows, liquidity_rows, pair_created = self._market_series(
                    con, files, asset_address, naive_ts
                )
                holder_count, holder_growth, concentration = (
                    self._holder_features_from_lake(
                        con, files, asset_address, naive_ts
                    )
                )
                flag_count = self._flag_count_from_lake(
                    con, files, asset_address, naive_ts
                )
            finally:
                con.close()
        return LakeReconstruction(
            market_rows=market_rows,
            liquidity_rows=liquidity_rows,
            pair_created_at=pair_created,
            holder_count=holder_count,
            holder_growth=holder_growth,
            holder_concentration=concentration,
            suspicious_contract_flags=flag_count,
        )

    def _market_series(
        self,
        con: Any,
        files: list[str],
        asset_address: str,
        naive_ts: datetime,
    ) -> tuple[list[SimpleNamespace], list[SimpleNamespace], datetime | None]:
        """Reconstruct the market/liquidity series from the GeckoTerminal
        evidence (the body of the old ``_series_from_lake``)."""
        rows = con.execute(
            _RECONSTRUCT_SQL,
            {
                "files": files,
                "asset_address": asset_address,
                "decision_ts": naive_ts,
            },
        ).fetchall()
        columns = [description[0] for description in con.description]
        market_rows: list[SimpleNamespace] = []
        liquidity_rows: list[SimpleNamespace] = []
        pair_created: datetime | None = None
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            observed_at = ensure_utc(record["observed_at"])
            ts = ensure_utc(record["ts"])
            source_id = int(record["source_id"]) if record["source_id"] is not None else 0
            pair_created_at = _parse_iso(record["pool_created_at"])
            if pair_created_at is not None and (
                pair_created is None or pair_created_at < pair_created
            ):
                pair_created = pair_created_at
            pair_address = str(record["pair_address"])
            market_rows.append(
                SimpleNamespace(
                    ts=ts,
                    observed_at=observed_at,
                    source_id=source_id,
                    pair_id=pair_address,
                    price_usd=record["price_usd"],
                    volume_usd=record["volume_usd"],
                    buys=record["buys"],
                    sells=record["sells"],
                )
            )
            if record["reserve_usd"] is not None:
                liquidity_rows.append(
                    SimpleNamespace(
                        ts=ts,
                        observed_at=observed_at,
                        source_id=source_id,
                        reserve_usd=record["reserve_usd"],
                    )
                )
        return market_rows, liquidity_rows, pair_created

    def _holder_features_from_lake(
        self,
        con: Any,
        files: list[str],
        asset_address: str,
        naive_ts: datetime,
    ) -> tuple[float | None, float | None, float | None]:
        """Reconstruct holder_count / holder_growth / top_holder_concentration
        from the archived ``chain_rpc`` holder-snapshot evidence.

        Mirrors the SQL path's ``_holder_features``: holder rows are deduped
        per (hour, wallet) keeping the latest observation (the same
        ``insert_holder_once`` semantics), the latest hour snapshot supplies
        the count and top-10 concentration, and the most recent snapshot at
        least one hour earlier supplies the growth delta.
        """
        rows = con.execute(
            _HOLDER_RECONSTRUCT_SQL,
            {
                "files": files,
                "asset_address": asset_address,
                "decision_ts": naive_ts,
            },
        ).fetchall()
        columns = [description[0] for description in con.description]
        hours: dict[datetime, dict[str, tuple[datetime, float]]] = {}
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            ts = ensure_utc(record["ts"])
            observed_at = ensure_utc(record["observed_at"])
            supply = record["supply"]
            accounts = json.loads(record["accounts_json"] or "[]")
            bucket = hours.setdefault(ts, {})
            for account in accounts:
                address = str(account.get("address") or "")
                if not address:
                    continue
                balance = _balance_from_json(
                    account.get("uiAmountString") or account.get("uiAmount")
                )
                if balance is None:
                    continue
                pct = balance / supply if supply and supply > 0 else None
                current = bucket.get(address)
                if current is None or observed_at > current[0]:
                    bucket[address] = (observed_at, pct if pct is not None else 0.0)
        decision_ts = ensure_utc(naive_ts)
        timestamps = sorted(ts for ts in hours if ts <= decision_ts)
        if not timestamps:
            return None, None, None
        latest_ts = timestamps[-1]
        latest = hours[latest_ts]
        holder_count = float(len(latest))
        concentration = float(
            sum(sorted((pct for _, pct in latest.values()), reverse=True)[:10])
        )
        prior = [ts for ts in timestamps if ts <= decision_ts - timedelta(hours=1)]
        growth = None
        if prior:
            growth = holder_count - float(len(hours[prior[-1]]))
        return holder_count, growth, concentration

    def _flag_count_from_lake(
        self,
        con: Any,
        files: list[str],
        asset_address: str,
        naive_ts: datetime,
    ) -> float:
        """Count evidence-backed contract flags for the asset: the GeckoTerminal
        pool scans whose ``reserve_in_usd`` fell below the discovery liquidity
        threshold — each such scan created one ``low_liquidity``
        ``ContractFlag`` row in the SQL path."""
        rows = con.execute(
            _FLAG_RECONSTRUCT_SQL,
            {
                "files": files,
                "asset_address": asset_address,
                "decision_ts": naive_ts,
            },
        ).fetchall()
        columns = [description[0] for description in con.description]
        count = 0
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            reserve = record["reserve_usd"]
            if reserve is not None and reserve < self.settings.min_discovery_liquidity_usd:
                count += 1
        return float(count)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _balance_from_json(value: Any) -> float | None:
    """Parse a holder account balance from an RPC payload field."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out
