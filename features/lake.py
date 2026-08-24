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

The store surface is the same ``ArchiveStore`` the compactor writes, so this
works against the local disk lake (zero-container profile) and MinIO/S3
(docker profile) with no extra dependencies.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc
from features.factory import FeatureValue, compute_market_block
from ops.archive import ArchiveStore, make_store

log = get_logger(__name__)

# The market/liquidity feature names the lake path can serve. Everything else
# in the full feature set needs SQL-side state (holders, contract flags,
# narratives, forecasts, lifecycle) and is out of scope for a lake read.
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


@dataclass(frozen=True)
class LakeMarketSeries:
    market_rows: list[SimpleNamespace]
    liquidity_rows: list[SimpleNamespace]
    pair_created_at: datetime | None


class LakeFeatureFactory:
    """Computes the market/liquidity feature block from the Parquet lake.

    ``build_for_asset`` returns the same ``FeatureValue`` set the SQL path
    produces for those names (identical math via ``compute_market_block``), so
    the two read paths are interchangeable for the lake-covered features.
    """

    def __init__(
        self,
        store: ArchiveStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or make_store(self.settings)

    def build_for_asset(
        self, *, asset_address: str, decision_ts: datetime
    ) -> dict[str, FeatureValue]:
        series = self._series_from_lake(asset_address, decision_ts)
        asset = SimpleNamespace(
            first_seen_at=series.pair_created_at or ensure_utc(decision_ts)
        )
        pairs = (
            [SimpleNamespace(created_at_source=series.pair_created_at)]
            if series.pair_created_at is not None
            else []
        )
        return compute_market_block(
            asset=asset,
            pairs=pairs,
            market_rows=series.market_rows,
            liquidity_rows=series.liquidity_rows,
            decision_ts=decision_ts,
        )

    def _series_from_lake(
        self, asset_address: str, decision_ts: datetime
    ) -> LakeMarketSeries:
        """Reconstruct the normalized market/liquidity series via DuckDB."""
        keys = self.store.list_objects(self.settings.archive_prefix)
        if not keys:
            return LakeMarketSeries([], [], None)
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
                rows = con.execute(
                    _RECONSTRUCT_SQL,
                    {
                        "files": files,
                        "asset_address": asset_address,
                        "decision_ts": naive_ts,
                    },
                ).fetchall()
                columns = [description[0] for description in con.description]
            finally:
                con.close()

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
        return LakeMarketSeries(market_rows, liquidity_rows, pair_created)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None
