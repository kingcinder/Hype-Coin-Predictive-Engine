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

The lake's first-wins dedup is additionally constrained to observations at
or after a pair's creation in SQL (``Pair.created_at``), supplied by the
caller as ``pair_created_at``: a snapshot can only be written after its
pair exists, so an observation that predates creation can never be the row
SQL's first-wins snapshot represents. The same bound applies to the
low-liquidity flag scans. When no bounds are supplied the reconstruction
is unconstrained (the pre-fix behavior); the parity job, the lake replay
path, and backtests supply them.

Beyond the market/liquidity block, the lake path reconstructs the on-chain
holder and contract-flag features from the archived RPC evidence payloads:

- ``holder_count`` / ``holder_growth`` / ``top_holder_concentration`` come
  from ``chain_rpc`` holder-snapshot evidence (the ``solana_rpc``
  ``largest_accounts`` + ``supply`` payloads for Solana and the
  ``evm_holders`` GoPlus token-security payloads for Base/Ethereum — both of
  which the SQL path turns into ``Holder`` rows), deduped per (hour, wallet)
  exactly like ``insert_holder_once``. When the payload carries the exact
  ``holder_count`` GoPlus reports, that count is used; older evidence falls
  back to the ranked-list length. Top-10 concentration always comes from the
  ranked list, and growth is normalized to an hourly rate. The reconstruction
  is chain-agnostic: it keys on
  ``source_type='chain_rpc'`` plus the ``mint`` / ``supply`` /
  ``largest_accounts`` payload shape, so an asset on any chain replays the
  same way.
- ``suspicious_contract_flags`` counts the evidence-backed contract flags:
  the GeckoTerminal pool scans whose ``reserve_in_usd <
  min_discovery_liquidity_usd`` created a ``low_liquidity`` ``ContractFlag``
  row, PLUS the contract analyzer's findings (honeypot patterns, mint
  authority, ownership not renounced, rug deployer) persisted as
  ``contract_analysis`` evidence — the same ``ContractFlag`` rows the SQL
  path's ``_contract_flag_count`` counts.

The store surface is the same ``ArchiveStore`` the compactor writes, so this
works against the local disk lake (zero-container profile) and MinIO/S3
(docker profile) with no extra dependencies.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc, floor_to_hour
from features.factory import FeatureValue, _feature, compute_market_block
from ops.archive import ArchiveStore, make_store
from storage import models
from storage.repository import upsert_features
from validation.leakage_guard import check_rows_point_in_time, require_decision_ts

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
    SELECT
        observed_at,
        source_id,
        payload_json,
        json_extract_string(payload_json, '$.new_pools') IS NOT NULL AS is_new_pools_feed,
        json_extract_string(payload_json, '$.attributes.address') IS NOT NULL AS is_pool_payload,
        json_extract_string(payload_json, '$.pairAddress') IS NOT NULL AS is_dex_pair_payload
    FROM read_parquet($files, union_by_name = true)
    WHERE source_type = 'market_data'
      AND observed_at <= $decision_ts
      AND (
          json_extract_string(payload_json, '$.new_pools') IS NOT NULL
          OR json_extract_string(payload_json, '$.attributes.address') IS NOT NULL
          OR json_extract_string(payload_json, '$.pairAddress') IS NOT NULL
      )
),
pools AS (
    SELECT
        r.observed_at AS observed_at,
        r.source_id AS source_id,
        r.is_new_pools_feed AS is_new_pools_feed,
        t.pool AS pool
    FROM raw AS r
    CROSS JOIN UNNEST(
        from_json(json_extract_string(r.payload_json, '$.new_pools'), '["JSON"]')
    ) AS t(pool)
    WHERE r.is_new_pools_feed
    UNION ALL
    SELECT
        r.observed_at AS observed_at,
        r.source_id AS source_id,
        FALSE AS is_new_pools_feed,
        r.payload_json AS pool
    FROM raw AS r
    WHERE NOT r.is_new_pools_feed AND r.is_pool_payload
    UNION ALL
    -- DexScreener returns one pair per payload with its own field names; map
    -- it onto the GeckoTerminal shape the rest of this query consumes.
    SELECT
        r.observed_at AS observed_at,
        r.source_id AS source_id,
        FALSE AS is_new_pools_feed,
        json_object(
            'attributes', json_object(
                'address', json_extract_string(r.payload_json, '$.pairAddress'),
                'base_token_price_usd', json_extract_string(r.payload_json, '$.priceUsd'),
                -- Volume mirrors the DexScreener normalizer's ``or`` chain
                -- (``volume.h1 or volume.h24 or volume.m5``): the first
                -- nonzero window wins; with no nonzero window the result is
                -- the m5 value -- 0.0 when m5 is present-but-zero, NULL only
                -- when every window is missing -- because SQL stores an
                -- all-zero payload as 0.0. The computed volume is emitted
                -- as h1 (h24/m5 NULL); ``norm`` only reads the COALESCE of
                -- the three windows, so the value is what the SQL path has.
                -- The DexScreener payload carries JSON numbers, so ``<> 0``
                -- is the right truthiness test for the ``or`` chain.
                'volume_usd', json_object(
                    'h1', CASE
                        WHEN COALESCE(
                            TRY_CAST(
                                json_extract_string(r.payload_json, '$.volume.h1') AS DOUBLE
                            ),
                            0
                        ) <> 0 THEN json_extract_string(r.payload_json, '$.volume.h1')
                        WHEN COALESCE(
                            TRY_CAST(
                                json_extract_string(r.payload_json, '$.volume.h24') AS DOUBLE
                            ),
                            0
                        ) <> 0 THEN json_extract_string(r.payload_json, '$.volume.h24')
                        WHEN COALESCE(
                            TRY_CAST(
                                json_extract_string(r.payload_json, '$.volume.m5') AS DOUBLE
                            ),
                            0
                        ) <> 0 THEN json_extract_string(r.payload_json, '$.volume.m5')
                        WHEN json_extract_string(r.payload_json, '$.volume.m5') IS NOT NULL
                            AND json_extract_string(r.payload_json, '$.volume.m5') <> ''
                            THEN '0'
                        ELSE NULL
                    END,
                    'h24', NULL,
                    'm5', NULL
                ),
                'transactions', json_object(
                    'h1', json_object(
                        'buys', json_extract_string(r.payload_json, '$.txns.h1.buys'),
                        'sells', json_extract_string(r.payload_json, '$.txns.h1.sells')
                    ),
                    'h24', json_object(
                        'buys', json_extract_string(r.payload_json, '$.txns.h24.buys'),
                        'sells', json_extract_string(r.payload_json, '$.txns.h24.sells')
                    ),
                    'm5', json_object(
                        'buys', json_extract_string(r.payload_json, '$.txns.m5.buys'),
                        'sells', json_extract_string(r.payload_json, '$.txns.m5.sells')
                    )
                ),
                'reserve_in_usd', json_extract_string(r.payload_json, '$.liquidity.usd'),
                'pool_created_at', strftime(
                    to_timestamp(
                        TRY_CAST(
                            json_extract_string(r.payload_json, '$.pairCreatedAt') AS BIGINT
                        ) / 1000.0
                    ) AT TIME ZONE 'UTC',
                    '%Y-%m-%dT%H:%M:%SZ'
                )
            ),
            'relationships', json_object(
                'base_token', json_object(
                    'data', json_object(
                        'id',
                        'dex_' || json_extract_string(r.payload_json, '$.baseToken.address')
                    )
                )
            )
        ) AS pool
    FROM raw AS r
    WHERE r.is_dex_pair_payload
),
norm AS (
    SELECT
        observed_at,
        source_id,
        is_new_pools_feed,
        date_trunc('hour', observed_at) AS ts,
        split_part(
            json_extract_string(pool, '$.relationships.base_token.data.id'), '_', 2
        ) AS base_address,
        json_extract_string(pool, '$.attributes.address') AS pair_address,
        json_extract_string(pool, '$.attributes.pool_created_at') AS pool_created_at,
        TRY_CAST(json_extract_string(pool, '$.attributes.base_token_price_usd') AS DOUBLE)
            AS price_usd,
        COALESCE(
            TRY_CAST(json_extract_string(pool, '$.attributes.volume_usd.h1') AS DOUBLE),
            TRY_CAST(json_extract_string(pool, '$.attributes.volume_usd.h24') AS DOUBLE),
            TRY_CAST(json_extract_string(pool, '$.attributes.volume_usd.m5') AS DOUBLE)
        ) AS volume_usd,
        COALESCE(
            TRY_CAST(json_extract_string(pool, '$.attributes.transactions.h1.buys') AS BIGINT),
            TRY_CAST(json_extract_string(pool, '$.attributes.transactions.h24.buys') AS BIGINT),
            TRY_CAST(json_extract_string(pool, '$.attributes.transactions.m5.buys') AS BIGINT)
        ) AS buys,
        COALESCE(
            TRY_CAST(json_extract_string(pool, '$.attributes.transactions.h1.sells') AS BIGINT),
            TRY_CAST(json_extract_string(pool, '$.attributes.transactions.h24.sells') AS BIGINT),
            TRY_CAST(json_extract_string(pool, '$.attributes.transactions.m5.sells') AS BIGINT)
        ) AS sells,
        TRY_CAST(json_extract_string(pool, '$.attributes.reserve_in_usd') AS DOUBLE)
            AS reserve_usd
    FROM pools
    WHERE base_address IS NOT NULL
      AND pair_address IS NOT NULL
      AND LIST_CONTAINS($asset_addresses, base_address)
),
-- A snapshot can only be written after its pair exists in SQL, so the lake
-- must not select observations that predate the pair's creation: the
-- earliest observation at/after ``Pair.created_at`` is the one SQL's
-- first-wins snapshot represents. ``$pair_bound_keys`` / ``$pair_bound_values``
-- carry the map (pool address -> earliest SQL ``Pair.created_at``, naive
-- UTC); a pair absent from the map, or an empty map, leaves that pair
-- unconstrained (the pre-fix behavior).
bounds AS (
    SELECT MAP($pair_bound_keys, $pair_bound_values) AS pair_bounds
),
bounded AS (
    SELECT norm.*
    FROM norm, bounds
    WHERE bounds.pair_bounds[pair_address] IS NULL
        OR observed_at >= bounds.pair_bounds[pair_address]
),
-- One row per (pair, hour, source), matching insert_market_snapshot_once.
-- One row per (pair, hour, source), matching insert_market_snapshot_once's
-- first-wins dedup. Both ingestion shapes are represented: the aggregate
-- ``new_pools`` feed and the per-pool payload, whichever was observed first.
deduped AS (
    SELECT *
    FROM (
        SELECT
            *,
            row_number() OVER (
                PARTITION BY pair_address, ts, source_id
                ORDER BY observed_at
            ) AS rn
        FROM bounded
    )
    WHERE rn = 1
)
SELECT base_address, observed_at, source_id, ts, pair_address, pool_created_at,
       price_usd, volume_usd, buys, sells, reserve_usd
FROM deduped
ORDER BY ts, source_id, observed_at
"""


_HOLDER_RECONSTRUCT_SQL = """
WITH raw AS (
    SELECT observed_at, payload_json
    FROM read_parquet($files, union_by_name = true)
    WHERE source_type = 'chain_rpc'
      AND observed_at <= $decision_ts
      AND json_extract_string(payload_json, '$.largest_accounts') IS NOT NULL
      AND LIST_CONTAINS(
          $asset_addresses,
          json_extract_string(payload_json, '$.mint')
      )
)
SELECT
    observed_at,
    json_extract_string(payload_json, '$.mint') AS asset_address,
    date_trunc('hour', observed_at) AS ts,
    TRY_CAST(json_extract_string(payload_json, '$.supply') AS DOUBLE) AS supply,
    TRY_CAST(json_extract_string(payload_json, '$.holder_count') AS BIGINT)
        AS holder_count,
    json_extract_string(payload_json, '$.largest_accounts') AS accounts_json
FROM raw
ORDER BY observed_at
"""

_CONTRACT_ANALYSIS_SQL = """
WITH raw AS (
    SELECT observed_at, payload_json
    FROM read_parquet($files, union_by_name = true)
    WHERE source_type = 'chain_rpc'
      AND observed_at <= $decision_ts
      AND json_extract_string(payload_json, '$.contract_analysis') IS NOT NULL
      AND LIST_CONTAINS(
          $asset_addresses,
          json_extract_string(payload_json, '$.contract_analysis.asset_address')
      )
)
SELECT
    observed_at,
    json_extract_string(payload_json, '$.contract_analysis.asset_address')
        AS asset_address,
    json_extract_string(payload_json, '$.contract_analysis.findings')
        AS findings_json
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
-- One row per feed-envelope pool item. The feed envelope is the flag count's
-- unit: one envelope item per poll per pool, and its content changes each
-- poll, so (unlike per-pool payloads, whose unchanged repeats collapse
-- under the compactor's content-hash dedupe) the envelope count tracks the
-- number of processing passes -- which is what SQL's per-pass
-- low_liquidity flag count is. Per-pool payloads are deliberately NOT
-- counted: the same scan's individual pool payload would double-count, and
-- unchanged per-pool repeats undercount. Residual gaps: pools past the
-- feed window (per-pool polls keep flagging after the feed stops listing
-- them) and re-processings of the same evidence (SQL flags again; the lake
-- has no new row). The flag count is deliberately NOT bounded by pair
-- creation: flags reference the contract, not the pair, so a legitimate
-- flag can predate ``Pair.created_at`` (the pair row may be inserted by a
-- later pass); bounding them created 13 new undercounts on 2026-11-16.
SELECT
    observed_at,
    split_part(
        json_extract_string(pool, '$.relationships.base_token.data.id'), '_', 2
    ) AS base_address,
    TRY_CAST(json_extract_string(pool, '$.attributes.reserve_in_usd') AS DOUBLE)
        AS reserve_usd
FROM pools
WHERE base_address IS NOT NULL
  AND LIST_CONTAINS($asset_addresses, base_address)
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
    suspicious_contract_flags: float | None


# Same-pass processing lag allowance for the pair-creation bound. A
# snapshot's ``observed_at`` is stamped when its payload is received; the
# ``Pair`` row is inserted a few milliseconds later in the same ingestion
# pass, so the first snapshot for a pair can predate ``Pair.created_at`` by
# that lag. Measured on the lab DB (3,355 pairs): the first snapshot's
# observed_at is within 85ms before ``Pair.created_at`` (median 0.000s), while
# genuinely pre-creation observations are a poll interval (>= 30s) earlier.
# 5s sits cleanly between the two, so the bound keeps same-pass rows and
# still excludes pre-creation rows.
PAIR_CREATION_BOUND_EPSILON = timedelta(seconds=5)


def load_pair_creation_bounds(
    session: Session, *, asset_ids: list[int] | None = None
) -> dict[str, datetime]:
    """Earliest SQL ``Pair.created_at`` per pool address.

    A snapshot can only be written after its pair exists in SQL, so the
    lake's first-wins dedup must not select observations that predate the
    pair's creation. The bound is the earliest ``Pair.created_at`` for the
    pool address (several ``Pair`` rows can share one pool address when the
    pair was re-created), less ``PAIR_CREATION_BOUND_EPSILON`` for the
    same-pass processing lag (see above). Returned datetimes are aware UTC;
    a pool address with no pair row is absent, and a pair absent from the map
    is unconstrained in the reconstruction.
    """
    stmt = (
        select(models.Pool.address, func.min(models.Pair.created_at))
        .join(models.Pair, models.Pair.pool_id == models.Pool.id)
        .group_by(models.Pool.address)
    )
    if asset_ids is not None:
        stmt = stmt.where(models.Pair.base_asset_id.in_(asset_ids))
    return {
        str(address): ensure_utc(created) - PAIR_CREATION_BOUND_EPSILON
        for address, created in session.execute(stmt).all()
        if address and created is not None
    }


def _naive_pair_bounds(
    pair_created_at: Mapping[str, datetime] | None,
) -> dict[str, datetime]:
    """Pair-creation bounds as naive UTC datetimes (the lake's TIMESTAMP type)."""
    if not pair_created_at:
        return {}
    return {
        address: ensure_utc(created).replace(tzinfo=None)
        for address, created in pair_created_at.items()
        if created is not None
    }


def _bounds_fingerprint(pair_created_at: Mapping[str, datetime] | None) -> str | None:
    """Short stable fingerprint of the pair-creation bounds for the cache key.

    The reconstruction depends on the bounds, so two reconstructions of the
    same (store, asset, hour) with different bounds must not share a cache
    entry. ``None`` when no bounds are supplied.
    """
    if not pair_created_at:
        return None
    digest = hashlib.sha256()
    for address in sorted(pair_created_at):
        digest.update(address.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(ensure_utc(pair_created_at[address]).isoformat().encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


class LakeFeatureFactory:
    """Computes the lake-covered feature block from the Parquet lake: the
    market/liquidity series plus the on-chain holder and contract-flag
    features.

    ``build_for_asset`` returns the same ``FeatureValue`` set the SQL path
    produces for those names (identical math via ``compute_market_block``,
    identical holder/flag reconstruction rules), so the two read paths are
    interchangeable for the lake-covered features.

    Reconstructions are cached at the class level, keyed by
    ``(store, asset address, hour, pair-creation-bounds fingerprint)``:
    repeated backtest steps over the same
    window create a fresh ``LakeFeatureFactory`` every hour, and a per-instance
    cache would be useless. The cache is a bounded LRU, is exact when the
    decision lands on an hour boundary (the backtest contract — ``hours_between``
    steps on the hour; ``build_and_persist_features`` replays those exact
    times), and is bypassed entirely for sub-hour decision times so they never
    reuse a stale within-hour snapshot. The archived lake is treated as
    immutable within the process (the replay contract) — call
    :meth:`clear_cache` if new evidence is compacted into the lake mid-process.
    """

    # Class-level ``(store, asset_address, hour, bounds fingerprint) ->
    # LakeReconstruction`` cache, shared across factory instances,
    # bounded via LRU eviction. The fingerprint is ``None`` when the
    # reconstruction ran unconstrained.
    _cache: OrderedDict[tuple[object, str, datetime, str | None], LakeReconstruction] = (
        OrderedDict()
    )
    _cache_lock = threading.RLock()
    cache_max_entries: int = 4096
    # Process-lifetime cache counters: a hit serves a reconstruction from the
    # cache (one saved DuckDB pass); a miss computes a fresh reconstruction.
    # Surfaced in backtest run metrics so replay runs can report how many
    # DuckDB queries the cache saved.
    _cache_hits: int = 0
    _cache_misses: int = 0

    @classmethod
    def clear_cache(cls) -> None:
        """Drop all cached reconstructions (e.g. after new evidence is
        compacted into the lake)."""
        with cls._cache_lock:
            cls._cache.clear()

    @classmethod
    def cache_stats(cls) -> dict[str, int]:
        """Cumulative cache hit/miss counters since the last reset."""
        with cls._cache_lock:
            return {
                "hits": cls._cache_hits,
                "misses": cls._cache_misses,
                # Every hit means one reconstruction that didn't need a fresh
                # DuckDB pass (the ``(asset, hour)`` unit the cache is keyed
                # on), so hits == saved queries.
                "saved_queries": cls._cache_hits,
            }

    @classmethod
    def reset_cache_stats(cls) -> None:
        """Zero the hit/miss counters (e.g. before a backtest run that wants a
        per-run readout)."""
        with cls._cache_lock:
            cls._cache_hits = 0
            cls._cache_misses = 0

    @classmethod
    def _count_hit(cls) -> None:
        with cls._cache_lock:
            cls._cache_hits += 1

    @classmethod
    def _count_miss(cls, n: int = 1) -> None:
        with cls._cache_lock:
            cls._cache_misses += n

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

    @require_decision_ts
    def build_for_asset(
        self,
        *,
        asset_address: str,
        decision_ts: datetime,
        pair_created_at: Mapping[str, datetime] | None = None,
    ) -> dict[str, FeatureValue]:
        """Reconstruct the lake-covered features for one asset: the
        market/liquidity block plus the on-chain holder and contract-flag
        features, with the same missing semantics the SQL path reports.

        ``pair_created_at`` maps pool address -> earliest SQL
        ``Pair.created_at`` (see :func:`load_pair_creation_bounds`); it
        constrains the lake's first-wins dedup to observations at/after pair
        creation, matching the SQL snapshot path. ``None`` reconstructs
        unconstrained (the pre-fix behavior).
        """
        recon = self._reconstruct(asset_address, decision_ts, pair_created_at)
        check_rows_point_in_time(recon.market_rows, decision_ts, feature_name="lake_market_block")
        check_rows_point_in_time(
            recon.liquidity_rows, decision_ts, feature_name="lake_market_block"
        )
        return self._values_for_reconstruction(recon, decision_ts)

    @require_decision_ts
    def build_for_assets(
        self,
        addresses: list[str],
        decision_ts: datetime,
        pair_created_at: Mapping[str, datetime] | None = None,
    ) -> dict[str, dict[str, FeatureValue]]:
        """Reconstruct the lake-covered block for many assets in ONE pass: the
        Parquet lake is downloaded once and all assets are queried in a single
        DuckDB session, so a full-lake job (the daily parity CI, backtest
        replay steps) is not O(assets) downloads.

        Honors the class-level ``(asset, hour)`` cache for hour-boundary
        decisions (the backtest contract); sub-hour decision times always
        compute fresh. The cache key also carries a fingerprint of
        ``pair_created_at``, so a bounded and an unbounded reconstruction of
        the same (store, asset, hour) never share an entry.
        """
        decision_ts = ensure_utc(decision_ts)
        hour = floor_to_hour(decision_ts)
        cacheable = decision_ts == hour
        store_key = self._store_key()
        bounds_fingerprint = _bounds_fingerprint(pair_created_at)
        cached: dict[str, LakeReconstruction] = {}
        todo: list[str] = []
        if cacheable:
            with self._cache_lock:
                for address in addresses:
                    entry = self._cache.get((store_key, address, hour, bounds_fingerprint))
                    if entry is not None:
                        cached[address] = entry
                        self._count_hit()
                    else:
                        todo.append(address)
        else:
            todo = list(addresses)
        recons: dict[str, LakeReconstruction] = dict(cached)
        if todo:
            batch = self._reconstruct_batch(todo, decision_ts, pair_created_at)
            recons.update(batch)
            self._count_miss(len(todo))
            if cacheable:
                with self._cache_lock:
                    for address, recon in batch.items():
                        if len(self._cache) >= self.cache_max_entries:
                            self._cache.popitem(last=False)
                        self._cache[(store_key, address, hour, bounds_fingerprint)] = recon
        # Point-in-time enforcement (plan #8): defensive second net behind
        # the ``observed_at <= $decision_ts`` SQL filters - a future-observed
        # row fails the replay instead of leaking into the features.
        for recon in recons.values():
            check_rows_point_in_time(
                recon.market_rows, decision_ts, feature_name="lake_market_block"
            )
            check_rows_point_in_time(
                recon.liquidity_rows, decision_ts, feature_name="lake_market_block"
            )
        return {
            address: self._values_for_reconstruction(recons[address], decision_ts)
            for address in addresses
        }

    def _values_for_reconstruction(
        self, recon: LakeReconstruction, decision_ts: datetime
    ) -> dict[str, FeatureValue]:
        """Turn one asset's reconstruction into the FeatureValue set."""
        asset = SimpleNamespace(first_seen_at=recon.pair_created_at or ensure_utc(decision_ts))
        pairs = (
            [
                SimpleNamespace(
                    created_at_source=recon.pair_created_at,
                    created_at=ensure_utc(decision_ts),
                )
            ]
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
        values["suspicious_contract_flags"] = _feature(
            "suspicious_contract_flags",
            recon.suspicious_contract_flags,
            1 if recon.suspicious_contract_flags is not None else 0,
            1.0 if recon.suspicious_contract_flags is not None else 0.0,
        )
        return values

    def persist_for_assets(
        self,
        session: Session,
        *,
        decision_ts: datetime,
        asset_ids: list[int] | None = None,
        pair_created_at: Mapping[str, datetime] | None = None,
    ) -> dict[int, dict[str, FeatureValue]]:
        """Compute the lake-covered block (market/liquidity + holder +
        contract flags) for each asset from the archived Parquet lake and
        persist it as ``Feature`` rows.

        This is the ``feature_source="lake"`` replay path: it reads DuckDB
        over the archived evidence and writes feature rows exactly like the
        SQL path (same batched ``upsert_features``), without touching any live
        normalized tables. All assets are reconstructed in one download /
        one DuckDB session per decision time.

        When ``pair_created_at`` is not supplied it is loaded from the
        session, so replay rows match the SQL path's pair-creation bound.
        """
        stmt = select(models.Asset)
        if asset_ids is not None:
            stmt = stmt.where(models.Asset.id.in_(asset_ids))
        assets = session.scalars(stmt).all()
        if not assets:
            return {}
        bounds = (
            pair_created_at
            if pair_created_at is not None
            else load_pair_creation_bounds(session, asset_ids=[asset.id for asset in assets])
        )
        values_by_asset = self.build_for_assets(
            [asset.address for asset in assets], decision_ts, pair_created_at=bounds
        )
        output: dict[int, dict[str, FeatureValue]] = {}
        for asset in assets:
            values = values_by_asset[asset.address]
            covered = [
                value for value in values.values() if value.name != "suspicious_contract_flags"
            ]
            has_tradable_pair = session.scalar(
                select(func.count())
                .select_from(models.Pair)
                .where(models.Pair.base_asset_id == asset.id)
            )
            if has_tradable_pair and covered and all(value.missing for value in covered):
                raise RuntimeError(
                    "archived evidence missing for requested asset-hour "
                    f"asset_id={asset.id} decision_ts={decision_ts.isoformat()}"
                )
            output[asset.id] = values
            upsert_features(
                session,
                asset_id=asset.id,
                decision_ts=decision_ts,
                values=values.values(),
            )
        return output

    def _series_from_lake(
        self,
        asset_address: str,
        decision_ts: datetime,
        pair_created_at: Mapping[str, datetime] | None = None,
    ) -> LakeMarketSeries:
        """Reconstruct the normalized market/liquidity series via DuckDB."""
        recon = self._reconstruct(asset_address, decision_ts, pair_created_at)
        return LakeMarketSeries(recon.market_rows, recon.liquidity_rows, recon.pair_created_at)

    def _reconstruct(
        self,
        asset_address: str,
        decision_ts: datetime,
        pair_created_at: Mapping[str, datetime] | None = None,
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
            self._count_miss()
            return self._reconstruct_uncached(asset_address, decision_ts, pair_created_at)
        key = (
            self._store_key(),
            asset_address,
            hour,
            _bounds_fingerprint(pair_created_at),
        )
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            self._count_hit()
            return cached
        recon = self._reconstruct_uncached(asset_address, decision_ts, pair_created_at)
        with self._cache_lock:
            if len(self._cache) >= self.cache_max_entries:
                self._cache.popitem(last=False)
            self._cache[key] = recon
        self._count_miss()
        return recon

    def _reconstruct_uncached(
        self,
        asset_address: str,
        decision_ts: datetime,
        pair_created_at: Mapping[str, datetime] | None = None,
    ) -> LakeReconstruction:
        """Reconstruct one asset, delegating to the batched run."""
        return self._reconstruct_batch([asset_address], decision_ts, pair_created_at)[asset_address]

    def _reconstruct_batch(
        self,
        addresses: list[str],
        decision_ts: datetime,
        pair_created_at: Mapping[str, datetime] | None = None,
    ) -> dict[str, LakeReconstruction]:
        """Reconstruct many assets in ONE pass: list and download the Parquet
        lake once, then query all assets in a single DuckDB session.

        This is what keeps the full-lake parity job and the ``lake`` replay
        path at O(1) downloads instead of O(assets): every asset shares the
        same files, session, and scan.
        """
        if not addresses:
            return {}
        keys = self.store.list_objects(self.settings.archive_prefix)
        if not keys:
            empty = LakeReconstruction([], [], None, None, None, None, None)
            return {address: empty for address in addresses}
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
                market_by_asset = self._market_series_batch(
                    con, files, addresses, naive_ts, pair_created_at
                )
                holder_by_asset = self._holder_features_batch(con, files, addresses, naive_ts)
                flag_by_asset = self._flag_count_batch(con, files, addresses, naive_ts)
            finally:
                con.close()
        out: dict[str, LakeReconstruction] = {}
        for address in addresses:
            market_rows, liquidity_rows, pair_created = market_by_asset.get(address, ([], [], None))
            holder_count, holder_growth, concentration = holder_by_asset.get(
                address, (None, None, None)
            )
            out[address] = LakeReconstruction(
                market_rows=market_rows,
                liquidity_rows=liquidity_rows,
                pair_created_at=pair_created,
                holder_count=holder_count,
                holder_growth=holder_growth,
                holder_concentration=concentration,
                suspicious_contract_flags=flag_by_asset.get(address),
            )
        return out

    def _market_series(
        self,
        con: Any,
        files: list[str],
        asset_address: str,
        naive_ts: datetime,
        pair_created_at: Mapping[str, datetime] | None = None,
    ) -> tuple[list[SimpleNamespace], list[SimpleNamespace], datetime | None]:
        """Single-asset wrapper over the batched market-series reconstruction."""
        return self._market_series_batch(con, files, [asset_address], naive_ts, pair_created_at)[
            asset_address
        ]

    def _market_series_batch(
        self,
        con: Any,
        files: list[str],
        addresses: list[str],
        naive_ts: datetime,
        pair_created_at: Mapping[str, datetime] | None = None,
    ) -> dict[str, tuple[list[SimpleNamespace], list[SimpleNamespace], datetime | None]]:
        """Reconstruct the market/liquidity series for all assets in one query
        (the body of the old ``_series_from_lake``, batched).

        ``pair_created_at`` (pool address -> earliest SQL ``Pair.created_at``)
        bounds the first-wins dedup to observations at/after pair creation,
        matching the SQL snapshot path; an empty map leaves every pair
        unconstrained.
        """
        if not addresses:
            return {}
        bounds = _naive_pair_bounds(pair_created_at)
        bound_keys = sorted(bounds)
        rows = con.execute(
            _RECONSTRUCT_SQL,
            {
                "files": files,
                "asset_addresses": addresses,
                "decision_ts": naive_ts,
                "pair_bound_keys": bound_keys,
                "pair_bound_values": [bounds[key] for key in bound_keys],
            },
        ).fetchall()
        columns = [description[0] for description in con.description]
        out: dict[str, tuple[list[SimpleNamespace], list[SimpleNamespace], datetime | None]] = {
            address: ([], [], None) for address in addresses
        }
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            address = str(record["base_address"])
            if address not in out:
                continue
            observed_at = ensure_utc(record["observed_at"])
            ts = ensure_utc(record["ts"])
            source_id = int(record["source_id"]) if record["source_id"] is not None else 0
            row_pair_created_at = _parse_iso(record["pool_created_at"])
            market_rows, liquidity_rows, pair_created = out[address]
            if row_pair_created_at is not None and (
                pair_created is None or row_pair_created_at < pair_created
            ):
                pair_created = row_pair_created_at
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
            out[address] = (market_rows, liquidity_rows, pair_created)
        return out

    def _holder_features_from_lake(
        self,
        con: Any,
        files: list[str],
        asset_address: str,
        naive_ts: datetime,
    ) -> tuple[float | None, float | None, float | None]:
        """Single-asset wrapper over the batched holder reconstruction."""
        return self._holder_features_batch(con, files, [asset_address], naive_ts)[asset_address]

    def _holder_features_batch(
        self,
        con: Any,
        files: list[str],
        addresses: list[str],
        naive_ts: datetime,
    ) -> dict[str, tuple[float | None, float | None, float | None]]:
        """Reconstruct holder_count / holder_growth / top_holder_concentration
        for all assets from the archived ``chain_rpc`` holder-snapshot evidence
        in one query.

        Mirrors the SQL path's ``_holder_features``: holder rows are deduped
        per (hour, wallet) keeping the latest observation (the same
        ``insert_holder_once`` semantics), the payload's exact
        ``holder_count`` supplies the count when present (falling back to the
        ranked-list length), the top-10 concentration comes from the ranked
        list, and the most recent snapshot at least one hour earlier supplies
        the hourly growth rate.
        """
        if not addresses:
            return {}
        rows = con.execute(
            _HOLDER_RECONSTRUCT_SQL,
            {
                "files": files,
                "asset_addresses": addresses,
                "decision_ts": naive_ts,
            },
        ).fetchall()
        columns = [description[0] for description in con.description]
        by_asset: dict[str, dict[datetime, dict[str, tuple[datetime, float]]]] = {
            address: {} for address in addresses
        }
        exact_by_asset: dict[str, dict[datetime, tuple[datetime, float]]] = {
            address: {} for address in addresses
        }
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            address = str(record["asset_address"])
            if address not in by_asset:
                continue
            ts = ensure_utc(record["ts"])
            observed_at = ensure_utc(record["observed_at"])
            supply = record["supply"]
            accounts = json.loads(record["accounts_json"] or "[]")
            exact_count = record["holder_count"]
            if exact_count is not None:
                current_exact = exact_by_asset[address].get(ts)
                if current_exact is None or observed_at > current_exact[0]:
                    exact_by_asset[address][ts] = (observed_at, float(exact_count))
            bucket = by_asset[address].setdefault(ts, {})
            for account in accounts:
                wallet = str(account.get("address") or "")
                if not wallet:
                    continue
                balance = _balance_from_json(
                    account.get("uiAmountString") or account.get("uiAmount")
                )
                if balance is None:
                    continue
                pct = balance / supply if supply and supply > 0 else None
                current = bucket.get(wallet)
                if current is None or observed_at > current[0]:
                    bucket[wallet] = (observed_at, pct if pct is not None else 0.0)
        decision_ts = ensure_utc(naive_ts)
        out: dict[str, tuple[float | None, float | None, float | None]] = {}
        for address, hours in by_asset.items():
            exacts = exact_by_asset[address]
            exact_candidates = [
                (ts, observed, count)
                for ts, (observed, count) in exacts.items()
                if observed <= decision_ts
            ]
            if exact_candidates:
                # The exact GoPlus count carries its real observation time;
                # use that, not the hour bucket, for growth.
                latest_ts, latest_observed, exact_latest_count = max(
                    exact_candidates, key=lambda item: item[1]
                )
                latest = hours[latest_ts]
                holder_count = exact_latest_count
            else:
                timestamps = sorted(ts for ts in hours if ts <= decision_ts)
                if not timestamps:
                    out[address] = (None, None, None)
                    continue
                latest_ts = timestamps[-1]
                latest_observed = latest_ts
                latest = hours[latest_ts]
                holder_count = float(len(latest))
            concentration = float(
                sum(sorted((pct for _, pct in latest.values()), reverse=True)[:10])
            )
            exact_prior_candidates = [
                (ts, observed, count)
                for ts, (observed, count) in exacts.items()
                if ts < latest_ts and observed <= latest_observed - timedelta(hours=1)
            ]
            growth = None
            if exact_prior_candidates:
                prior_ts, prior_observed, prior_count = max(
                    exact_prior_candidates, key=lambda item: item[1]
                )
                elapsed_hours = (latest_observed - prior_observed).total_seconds() / 3600.0
                if elapsed_hours > 0:
                    growth = (holder_count - prior_count) / elapsed_hours
            elif not exact_candidates:
                prior = [
                    ts
                    for ts in timestamps
                    if ts < latest_ts and ts <= latest_observed - timedelta(hours=1)
                ]
                if prior:
                    prior_ts = prior[-1]
                    prior_count = float(len(hours[prior_ts]))
                    elapsed_hours = (latest_observed - prior_ts).total_seconds() / 3600.0
                    if elapsed_hours > 0:
                        growth = (holder_count - prior_count) / elapsed_hours
            out[address] = (holder_count, growth, concentration)
        return out

    def _flag_count_from_lake(
        self,
        con: Any,
        files: list[str],
        asset_address: str,
        naive_ts: datetime,
    ) -> float | None:
        """Single-asset wrapper over the batched flag count."""
        return self._flag_count_batch(con, files, [asset_address], naive_ts)[asset_address]

    def _flag_count_batch(
        self,
        con: Any,
        files: list[str],
        addresses: list[str],
        naive_ts: datetime,
    ) -> dict[str, float | None]:
        """Count evidence-backed contract flags per asset in one query: the
        ``new_pools`` feed items whose pool reserve fell below the discovery
        liquidity threshold (each feed poll creates one ``low_liquidity``
        ``ContractFlag`` row in the SQL path) PLUS the contract analyzer's
        findings archived as ``contract_analysis`` evidence — the same set of
        ``ContractFlag`` rows the SQL path's ``_contract_flag_count`` counts.

        The feed envelope is the count's unit (see ``_FLAG_RECONSTRUCT_SQL``):
        per-pool payloads are not counted. The count is deliberately not
        bounded by pair creation (flags reference the contract, not the pair).
        """
        if not addresses:
            return {}
        rows = con.execute(
            _FLAG_RECONSTRUCT_SQL,
            {
                "files": files,
                "asset_addresses": addresses,
                "decision_ts": naive_ts,
            },
        ).fetchall()
        columns = [description[0] for description in con.description]
        out: dict[str, float | None] = {}
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            address = str(record["base_address"])
            if address not in addresses:
                continue
            out.setdefault(address, 0.0)
            reserve = record["reserve_usd"]
            if reserve is not None and reserve < self.settings.min_discovery_liquidity_usd:
                out[address] = float(out.get(address) or 0.0) + 1.0
        analysis = self._analysis_flag_count_batch(con, files, addresses, naive_ts)
        for address, count in analysis.items():
            out[address] = float(out.get(address) or 0.0) + count
        return out

    def _analysis_flag_count_batch(
        self,
        con: Any,
        files: list[str],
        addresses: list[str],
        naive_ts: datetime,
    ) -> dict[str, float]:
        """Count the contract analyzer's findings per asset from the archived
        ``contract_analysis`` evidence payloads: honeypot patterns, mint
        authority, pause function, ownership not renounced, and rug deployer
        — each finding the SQL path recorded as one ``ContractFlag`` row.

        Mirrors the SQL path's severity filter (``warning | high | critical |
        black``), so a finding stored with a non-counted severity can never
        silently diverge the two read paths.
        """
        if not addresses:
            return {}
        rows = con.execute(
            _CONTRACT_ANALYSIS_SQL,
            {
                "files": files,
                "asset_addresses": addresses,
                "decision_ts": naive_ts,
            },
        ).fetchall()
        columns = [description[0] for description in con.description]
        out: dict[str, float] = {}
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            address = str(record["asset_address"])
            if address not in addresses:
                continue
            out.setdefault(address, 0.0)
            try:
                findings = json.loads(record["findings_json"] or "[]")
            except (TypeError, ValueError):
                findings = []
            out[address] += float(
                sum(
                    1
                    for finding in findings
                    if isinstance(finding, dict)
                    and finding.get("severity") in _COUNTED_FLAG_SEVERITIES
                )
            )
        return out


# The severities the SQL path's ``_contract_flag_count`` counts; the lake
# reconstruction applies the same filter so the two read paths count the same
# findings (a ``contract_analysis`` finding outside this set is recorded but
# never counted, exactly like a SQL-side flag row with that severity).
_COUNTED_FLAG_SEVERITIES = frozenset({"warning", "high", "critical", "black"})


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
