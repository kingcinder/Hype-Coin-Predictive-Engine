from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any, overload

import numpy as np
from sqlalchemy import Text, func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import IgnitionEventType, LifecyclePhase
from common.time import ensure_utc, utc_now
from features.definitions import FEATURE_NAMES
from pump_physics.engine import phase_rank
from storage import models
from storage.repository import upsert_feature


@dataclass(frozen=True)
class FeatureValue:
    name: str
    value: float
    missing: bool
    source_count: int = 0
    freshness_score: float = 0.0
    source_refs: dict[str, object] | None = None


@overload
def _safe_float(value: Any, default: None) -> float | None: ...


@overload
def _safe_float(value: Any, default: float = 0.0) -> float: ...


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except (TypeError, ValueError):
        return default


def _pct_return(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior <= 0:
        return None
    return (current / prior - 1.0) * 100.0


def _freshness(observed_at: datetime | None, decision_ts: datetime) -> float:
    if not observed_at:
        return 0.0
    age = max(0.0, (ensure_utc(decision_ts) - ensure_utc(observed_at)).total_seconds())
    return max(0.0, min(1.0, 1.0 - age / 86_400.0))


def _like_escape(value: str) -> str:
    """Escape LIKE metacharacters so a URL containing ``%`` or ``_`` cannot
    widen (or narrow) the ``LIKE '%...%'`` pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _latest_snapshot_before(rows: Sequence[Any], cutoff: datetime):
    candidates = [row for row in rows if ensure_utc(row.ts) <= ensure_utc(cutoff)]
    return max(candidates, key=lambda row: row.ts) if candidates else None


def _feature(
    name: str, value: float | None, source_count: int, freshness_score: float
) -> FeatureValue:
    if value is None:
        return FeatureValue(
            name=name,
            value=0.0,
            missing=True,
            source_count=source_count,
            freshness_score=freshness_score,
        )
    return FeatureValue(
        name=name,
        value=float(value),
        missing=False,
        source_count=source_count,
        freshness_score=freshness_score,
    )


def _load_source_names(session: Session) -> dict[int, str]:
    """Load the source id→name map in one query.

    The velocity features filter mentions by source name on every asset and
    the mapping is invariant within a scan, so the per-asset build path loads
    it lazily on first use and ``persist_for_assets`` loads it once and passes
    it down — turning O(assets) full-table scans of ``sources`` into one.
    """
    return {
        row.id: str(row.name)
        for row in session.execute(select(models.Source.id, models.Source.name)).all()
    }


def compute_market_block(
    *,
    asset: Any,
    pairs: Sequence[Any],
    market_rows: Sequence[Any],
    liquidity_rows: Sequence[Any],
    decision_ts: datetime,
) -> dict[str, FeatureValue]:
    """The market/liquidity feature block, shared by the SQL and lake read paths.

    Operates on duck-typed rows (``ts``, ``price_usd``, ``volume_usd``,
    ``buys``, ``sells``, ``observed_at``, ``source_id`` for market rows;
    ``ts``, ``reserve_usd``, ``observed_at`` for liquidity rows; pairs need
    ``created_at_source``; the asset needs ``first_seen_at``), so the SQL path
    passes ORM rows and the DuckDB lake path passes namespaces reconstructed
    from the Parquet lake. Both paths provably compute identical values.
    """
    decision_ts = ensure_utc(decision_ts)
    latest_market = max(market_rows, key=lambda row: row.ts) if market_rows else None
    latest_liquidity = max(liquidity_rows, key=lambda row: row.ts) if liquidity_rows else None

    current_price = _safe_float(latest_market.price_usd, None) if latest_market else None
    m5 = _latest_snapshot_before(market_rows, decision_ts - timedelta(minutes=5))
    h1 = _latest_snapshot_before(market_rows, decision_ts - timedelta(hours=1))
    latest_volume = _safe_float(latest_market.volume_usd, None) if latest_market else None
    previous_volumes = [
        _safe_float(row.volume_usd, 0.0)
        for row in market_rows
        if row.volume_usd is not None
        and row.ts < (latest_market.ts if latest_market else decision_ts)
    ][-12:]
    volume_accel = None
    if latest_volume is not None and previous_volumes:
        baseline = max(1.0, median(previous_volumes))
        volume_accel = latest_volume / baseline

    latest_liq = _safe_float(latest_liquidity.reserve_usd, None) if latest_liquidity else None
    prev_liq_row = _latest_snapshot_before(liquidity_rows, decision_ts - timedelta(hours=1))
    liquidity_change = _pct_return(
        latest_liq, _safe_float(prev_liq_row.reserve_usd, None) if prev_liq_row else None
    )

    buys = _safe_float(latest_market.buys, None) if latest_market else None
    sells = _safe_float(latest_market.sells, None) if latest_market else None
    buy_sell_ratio = (
        (buys + 1.0) / (sells + 1.0) if buys is not None and sells is not None else None
    )

    pair_age = None
    if pairs:
        first_pair_time = min(
            [pair.created_at_source for pair in pairs if pair.created_at_source]
            or [asset.first_seen_at]
        )
        # Point-in-time guard: a pair created AFTER the decision time did not
        # exist yet at the decision — the age must read as missing, not clamp
        # to 0.0 (which would silently report "newborn" for a pair that was
        # not even deployed). The lake path reports missing in this case, so
        # this guard keeps the SQL and lake read paths in parity.
        if first_pair_time is not None and ensure_utc(first_pair_time) <= ensure_utc(decision_ts):
            pair_age = max(0.0, (decision_ts - ensure_utc(first_pair_time)).total_seconds() / 60.0)

    venue_agreement = _venue_agreement(market_rows, decision_ts)
    volatility = _volatility(market_rows)
    spread_estimate = None if latest_liq is None else min(100.0, 1000.0 / max(latest_liq, 1.0))

    source_count = len({row.source_id for row in [*market_rows, *liquidity_rows]})
    market_freshness = _freshness(
        max([row.observed_at for row in market_rows], default=None), decision_ts
    )
    liquidity_freshness = _freshness(
        max([row.observed_at for row in liquidity_rows], default=None), decision_ts
    )

    values = [
        _feature(
            "five_min_return",
            _pct_return(current_price, _safe_float(m5.price_usd, None) if m5 else None),
            source_count,
            market_freshness,
        ),
        _feature(
            "one_hour_return",
            _pct_return(current_price, _safe_float(h1.price_usd, None) if h1 else None),
            source_count,
            market_freshness,
        ),
        _feature("volume_acceleration", volume_accel, source_count, market_freshness),
        _feature("liquidity_depth", latest_liq, source_count, liquidity_freshness),
        _feature("liquidity_change", liquidity_change, source_count, liquidity_freshness),
        _feature("buy_sell_ratio", buy_sell_ratio, source_count, market_freshness),
        _feature("unique_buyers_estimate", buys, source_count, market_freshness),
        _feature("pair_age_minutes", pair_age, source_count, market_freshness),
        _feature("spread_estimate", spread_estimate, source_count, liquidity_freshness),
        _feature("volatility", volatility, source_count, market_freshness),
        _feature("venue_agreement", venue_agreement, source_count, market_freshness),
    ]
    return {value.name: value for value in values}


def _venue_agreement(market_rows: Sequence[Any], decision_ts: datetime) -> float | None:
    latest_by_pair: dict[int, Any] = {}
    for row in market_rows:
        if ensure_utc(row.ts) <= ensure_utc(decision_ts) and row.price_usd is not None:
            current = latest_by_pair.get(row.pair_id)
            if current is None or ensure_utc(row.ts) > ensure_utc(current.ts):
                latest_by_pair[row.pair_id] = row
    prices = [_safe_float(row.price_usd, 0.0) for row in latest_by_pair.values() if row.price_usd]
    if len(prices) < 2:
        return 100.0 if len(prices) == 1 else None
    avg = float(np.mean(prices))
    if avg <= 0:
        return None
    dispersion = float(np.std(prices) / avg)
    return max(0.0, 100.0 * (1.0 - dispersion))


def _volatility(market_rows: Sequence[Any]) -> float | None:
    rows = [row for row in market_rows if row.price_usd and row.price_usd > 0][-24:]
    if len(rows) < 3:
        return None
    returns: list[float] = []
    for prev, current in zip(rows, rows[1:], strict=False):
        current_price = _safe_float(current.price_usd, None)
        previous_price = _safe_float(prev.price_usd, None)
        if current_price is None or previous_price is None or previous_price <= 0:
            continue
        returns.append(math.log(current_price / previous_price))
    if not returns:
        return None
    return float(np.std(returns) * 100.0)


class FeatureFactory:
    def build_for_asset(
        self,
        session: Session,
        asset: models.Asset,
        decision_ts: datetime,
        *,
        source_names: dict[int, str] | None = None,
    ) -> list[FeatureValue]:
        decision_ts = ensure_utc(decision_ts)
        pairs = session.scalars(
            select(models.Pair).where(models.Pair.base_asset_id == asset.id)
        ).all()
        pair_ids = [pair.id for pair in pairs]
        pool_ids = [pair.pool_id for pair in pairs if pair.pool_id is not None]

        market_rows: list[models.MarketSnapshot] = []
        if pair_ids:
            market_rows = list(
                session.scalars(
                    select(models.MarketSnapshot)
                    .where(
                        models.MarketSnapshot.pair_id.in_(pair_ids),
                        models.MarketSnapshot.observed_at <= decision_ts,
                    )
                    .order_by(models.MarketSnapshot.ts)
                )
            )
        liquidity_rows: list[models.LiquiditySnapshot] = []
        if pool_ids:
            liquidity_rows = list(
                session.scalars(
                    select(models.LiquiditySnapshot)
                    .where(
                        models.LiquiditySnapshot.pool_id.in_(pool_ids),
                        models.LiquiditySnapshot.observed_at <= decision_ts,
                    )
                    .order_by(models.LiquiditySnapshot.ts)
                )
            )
        market_block = compute_market_block(
            asset=asset,
            pairs=pairs,
            market_rows=market_rows,
            liquidity_rows=liquidity_rows,
            decision_ts=decision_ts,
        )

        holder_count, holder_growth, concentration = self._holder_features(
            session, asset.id, decision_ts
        )
        flags = self._contract_flag_count(session, asset.id, decision_ts)
        mention_velocity, narrative_acceleration = self._attention_features(
            session, asset, decision_ts
        )
        ignition_signal, withdrawal_signal = self._ignition_features(session, asset.id, decision_ts)
        lp_removal_signal = self._lp_removal_signal(session, asset.id, decision_ts)
        recidivism = self._recidivism_feature(session, asset.id, decision_ts)
        prelaunch_priority = self._prelaunch_feature(session, asset, decision_ts)
        catalyst_proximity = self._catalyst_proximity(session, asset.id, decision_ts)
        cluster_growth, channel_diversity, prelaunch_velocity = self._narrative_metrics(
            session, asset, decision_ts
        )
        kol_velocity, star_velocity, download_velocity = self._velocity_features(
            session, asset, decision_ts, source_names=source_names
        )
        rpc_pool_health = self._rpc_pool_health(session, asset, decision_ts)
        collapse_probability = self._forecast_probability(session, asset.id, decision_ts)
        website_presence = self._url_evidenced_before(session, asset.website_url, decision_ts)
        github_presence = self._url_evidenced_before(session, asset.github_url, decision_ts)

        lifecycle_phase = self._lifecycle_phase(session, asset.id, decision_ts)

        values = list(market_block.values()) + [
            _feature(
                "holder_count",
                holder_count,
                1 if holder_count is not None else 0,
                1.0 if holder_count is not None else 0.0,
            ),
            _feature(
                "holder_growth",
                holder_growth,
                1 if holder_growth is not None else 0,
                1.0 if holder_growth is not None else 0.0,
            ),
            _feature(
                "top_holder_concentration",
                concentration,
                1 if concentration is not None else 0,
                1.0 if concentration is not None else 0.0,
            ),
            _feature(
                "mention_velocity",
                mention_velocity,
                1 if mention_velocity is not None else 0,
                0.8 if mention_velocity is not None else 0.0,
            ),
            _feature(
                "website_presence",
                website_presence,
                1 if website_presence is not None else 0,
                1.0 if website_presence is not None else 0.0,
            ),
            _feature(
                "github_presence_public",
                github_presence,
                1 if github_presence is not None else 0,
                1.0 if github_presence is not None else 0.0,
            ),
            _feature("suspicious_contract_flags", flags, 1, 1.0),
            _feature(
                "deployer_history_available",
                self._deployer_history(session, asset.id, decision_ts),
                1,
                1.0,
            ),
            _feature(
                "narrative_acceleration",
                narrative_acceleration,
                1 if narrative_acceleration is not None else 0,
                0.8 if narrative_acceleration is not None else 0.0,
            ),
            _feature("ignition_signal", ignition_signal, 1, 1.0),
            _feature("liquidity_withdrawal_signal", withdrawal_signal, 1, 1.0),
            _feature("lp_removal_signal", lp_removal_signal, 1, 1.0),
            _feature(
                "recidivism_score",
                recidivism,
                1 if recidivism is not None else 0,
                1.0 if recidivism is not None else 0.0,
            ),
            _feature("prelaunch_priority", prelaunch_priority, 1, 1.0),
            _feature(
                "catalyst_proximity_hours",
                catalyst_proximity,
                1 if catalyst_proximity is not None else 0,
                1.0 if catalyst_proximity is not None else 0.0,
            ),
            _feature(
                "narrative_cluster_growth_7d",
                cluster_growth,
                1 if cluster_growth is not None else 0,
                1.0 if cluster_growth is not None else 0.0,
            ),
            _feature("shill_channel_diversity", channel_diversity, 1, 1.0),
            _feature("prelaunch_narrative_velocity", prelaunch_velocity, 1, 1.0),
            _feature(
                "kol_velocity",
                kol_velocity,
                1 if kol_velocity is not None else 0,
                0.8 if kol_velocity is not None else 0.0,
            ),
            _feature(
                "github_star_velocity",
                star_velocity,
                1 if star_velocity is not None else 0,
                0.7 if star_velocity is not None else 0.0,
            ),
            _feature(
                "hf_download_velocity",
                download_velocity,
                1 if download_velocity is not None else 0,
                0.7 if download_velocity is not None else 0.0,
            ),
            _feature("rpc_pool_health", rpc_pool_health, 1, 1.0),
            _feature(
                "collapse_probability_24h",
                collapse_probability,
                1 if collapse_probability is not None else 0,
                1.0 if collapse_probability is not None else 0.0,
            ),
            _feature(
                "lifecycle_phase",
                lifecycle_phase,
                1 if lifecycle_phase is not None else 0,
                1.0 if lifecycle_phase is not None else 0.0,
            ),
        ]
        assert {value.name for value in values} == set(FEATURE_NAMES)
        return values

    def persist_for_assets(
        self, session: Session, *, decision_ts: datetime, asset_ids: list[int] | None = None
    ) -> dict[int, dict[str, FeatureValue]]:
        stmt = select(models.Asset)
        if asset_ids is not None:
            stmt = stmt.where(models.Asset.id.in_(asset_ids))
        assets = session.scalars(stmt).all()
        output: dict[int, dict[str, FeatureValue]] = {}
        # The source id→name map is invariant within a scan: load it once and
        # share it across every per-asset build instead of reloading the whole
        # ``sources`` table inside each asset's velocity features.
        source_names = _load_source_names(session) if assets else {}
        for asset in assets:
            values = self.build_for_asset(session, asset, decision_ts, source_names=source_names)
            output[asset.id] = {value.name: value for value in values}
            for value in values:
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

    def _holder_features(
        self, session: Session, asset_id: int, decision_ts: datetime
    ) -> tuple[float | None, float | None, float | None]:
        latest_ts = session.scalar(
            select(func.max(models.Holder.ts)).where(
                models.Holder.asset_id == asset_id,
                models.Holder.observed_at <= decision_ts,
                models.Holder.ts <= decision_ts,
            )
        )
        if not latest_ts:
            return None, None, None
        holders = session.scalars(
            select(models.Holder).where(
                models.Holder.asset_id == asset_id, models.Holder.ts == latest_ts
            )
        ).all()
        holder_count = float(len(holders))
        top_concentration = sum(
            sorted([_safe_float(row.pct_supply, 0.0) for row in holders], reverse=True)[:10]
        )
        prior_ts = session.scalar(
            select(func.max(models.Holder.ts)).where(
                models.Holder.asset_id == asset_id,
                models.Holder.ts <= decision_ts - timedelta(hours=1),
                models.Holder.observed_at <= decision_ts,
            )
        )
        growth = None
        if prior_ts:
            prior_count = session.scalar(
                select(func.count())
                .select_from(models.Holder)
                .where(models.Holder.asset_id == asset_id, models.Holder.ts == prior_ts)
            )
            growth = holder_count - float(prior_count or 0)
        return holder_count, growth, top_concentration

    def _contract_flag_count(self, session: Session, asset_id: int, decision_ts: datetime) -> float:
        contract_ids = session.scalars(
            select(models.Contract.id).where(models.Contract.asset_id == asset_id)
        ).all()
        if not contract_ids:
            return 0.0
        return float(
            session.scalar(
                select(func.count())
                .select_from(models.ContractFlag)
                .where(
                    models.ContractFlag.contract_id.in_(contract_ids),
                    models.ContractFlag.observed_at <= decision_ts,
                    models.ContractFlag.severity.in_(["warning", "high", "critical", "black"]),
                )
            )
            or 0
        )

    def _deployer_history(self, session: Session, asset_id: int, decision_ts: datetime) -> float:
        """Count contracts with a known deployer observed at or before decision time.

        Gated on ``Contract.observed_at <= decision_ts`` so a contract that
        was only inspected after the decision time cannot leak into a
        historical feature snapshot.
        """
        return float(
            session.scalar(
                select(func.count())
                .select_from(models.Contract)
                .where(
                    models.Contract.asset_id == asset_id,
                    models.Contract.deployer_wallet.is_not(None),
                    models.Contract.observed_at <= decision_ts,
                )
            )
            or 0
        )

    def _url_evidenced_before(
        self, session: Session, url: str | None, decision_ts: datetime
    ) -> float | None:
        """Point-in-time website/github presence: 1.0 only when crawler evidence
        referencing the URL was observed at or before the decision time.

        The asset row's ``website_url``/``github_url`` reflect *current* state
        — a URL discovered by a Night Crawler last week would wrongly count
        for a decision a month ago.  Evidence-gating on ``observed_at <=
        decision_ts`` keeps the feature honest for historical snapshots.

        Returns ``None`` — read as "unknown" (missing) downstream — when a URL
        exists on the asset row but no evidence of it was observed at or before
        the decision time.  This is the point-in-time-correct answer for a
        decision that predates the URL's discovery: we cannot say the website
        was absent (it may simply not have been crawled yet), so a confident
        0.0 would be a silent zero, and a 1.0 would leak the live value.
        Only a URL that is absent from the asset row entirely (``url is
        None``) reads as a confident 0.0.

        Evidence sources: a ``SocialMention.raw_ref`` that contains the URL,
        or a ``RawEvidenceItem.payload`` whose JSON text mentions it (crawlers
        store the repo/website URLs inside the payload — the ``url_hash``
        column is a digest of the URL, not the URL itself, so it cannot be
        searched with LIKE).  LIKE metacharacters in the URL are escaped so a
        ``%`` or ``_`` in a link cannot widen the match.
        """
        if not url:
            return 0.0
        needle = _like_escape(url.lower())
        mention = session.scalar(
            select(func.count())
            .select_from(models.SocialMention)
            .where(
                models.SocialMention.observed_at <= decision_ts,
                models.SocialMention.raw_ref.is_not(None),
                func.lower(models.SocialMention.raw_ref).like(f"%{needle}%", escape="\\"),
            )
        )
        if mention:
            return 1.0
        evidence = session.scalar(
            select(func.count())
            .select_from(models.RawEvidenceItem)
            .where(
                models.RawEvidenceItem.observed_at <= decision_ts,
                models.RawEvidenceItem.payload.is_not(None),
                func.lower(func.cast(models.RawEvidenceItem.payload, Text)).like(
                    f"%{needle}%", escape="\\"
                ),
            )
        )
        if evidence:
            return 1.0
        return None

    def _ignition_features(
        self, session: Session, asset_id: int, decision_ts: datetime
    ) -> tuple[float, float]:
        window_start = decision_ts - timedelta(hours=24)
        ignition_count = (
            session.scalar(
                select(func.count())
                .select_from(models.IgnitionEvent)
                .where(
                    models.IgnitionEvent.asset_id == asset_id,
                    models.IgnitionEvent.event_type.in_(
                        [
                            IgnitionEventType.FIRST_LIQUIDITY_INJECTION.value,
                            IgnitionEventType.SNIPER_BURST.value,
                        ]
                    ),
                    models.IgnitionEvent.ts >= window_start,
                    models.IgnitionEvent.ts <= decision_ts,
                    models.IgnitionEvent.observed_at <= decision_ts,
                )
            )
            or 0
        )
        withdrawal_count = (
            session.scalar(
                select(func.count())
                .select_from(models.IgnitionEvent)
                .where(
                    models.IgnitionEvent.asset_id == asset_id,
                    models.IgnitionEvent.event_type == IgnitionEventType.LIQUIDITY_WITHDRAWAL.value,
                    models.IgnitionEvent.ts >= window_start,
                    models.IgnitionEvent.ts <= decision_ts,
                    models.IgnitionEvent.observed_at <= decision_ts,
                )
            )
            or 0
        )
        return (1.0 if ignition_count else 0.0), float(withdrawal_count)

    def _lp_removal_signal(self, session: Session, asset_id: int, decision_ts: datetime) -> float:
        """Count fresh on-chain LP burns/withdrawals before this decision."""
        count = session.scalar(
            select(func.count())
            .select_from(models.LiquidityRemovalEvent)
            .where(
                models.LiquidityRemovalEvent.asset_id == asset_id,
                models.LiquidityRemovalEvent.ts >= decision_ts - timedelta(hours=24),
                models.LiquidityRemovalEvent.ts <= decision_ts,
                models.LiquidityRemovalEvent.observed_at <= decision_ts,
            )
        )
        return float(count or 0)

    def _prelaunch_feature(
        self, session: Session, asset: models.Asset, decision_ts: datetime
    ) -> float:
        row = session.scalar(
            select(models.PrelaunchCandidate)
            .where(
                models.PrelaunchCandidate.asset_id == asset.id,
                models.PrelaunchCandidate.decision_ts <= decision_ts,
                models.PrelaunchCandidate.decision_ts >= decision_ts - timedelta(days=7),
            )
            .order_by(models.PrelaunchCandidate.decision_ts.desc())
            .limit(1)
        )
        return float(row.priority_score) if row else 0.0

    def _catalyst_proximity(
        self, session: Session, asset_id: int, decision_ts: datetime
    ) -> float | None:
        nearest = session.scalar(
            select(func.min(models.Catalyst.scheduled_at)).where(
                models.Catalyst.asset_id == asset_id,
                models.Catalyst.scheduled_at >= decision_ts,
                models.Catalyst.observed_at <= decision_ts,
            )
        )
        if nearest is None:
            return None
        return min(168.0, max(0.0, (nearest - decision_ts).total_seconds() / 3600.0))

    def _narrative_metrics(
        self, session: Session, asset: models.Asset, decision_ts: datetime
    ) -> tuple[float | None, float, float]:
        symbol_filter = (models.SocialMention.asset_id == asset.id) | (
            models.SocialMention.topic.ilike(f"%{asset.symbol}%")
        )
        recent = session.scalars(
            select(models.SocialMention).where(
                symbol_filter,
                models.SocialMention.observed_at <= decision_ts,
                models.SocialMention.ts > decision_ts - timedelta(hours=24),
            )
        ).all()
        channel_diversity = float(len({mention.source_id for mention in recent}))

        cluster_keys = {
            (mention.metrics_json or {}).get("cluster_key")
            for mention in recent
            if (mention.metrics_json or {}).get("cluster_key")
        }
        growth: float | None = None
        if cluster_keys:
            window_rows = session.scalars(
                select(models.SocialMention).where(
                    symbol_filter,
                    models.SocialMention.observed_at <= decision_ts,
                    models.SocialMention.ts > decision_ts - timedelta(days=14),
                )
            ).all()
            seven_days_ago = decision_ts - timedelta(days=7)
            for key in cluster_keys:
                current = sum(
                    1
                    for mention in window_rows
                    if ensure_utc(mention.ts) > seven_days_ago
                    and (mention.metrics_json or {}).get("cluster_key") == key
                )
                prior = sum(
                    1
                    for mention in window_rows
                    if ensure_utc(mention.ts) <= seven_days_ago
                    and (mention.metrics_json or {}).get("cluster_key") == key
                )
                ratio = current / max(1, prior)
                if growth is None or ratio > growth:
                    growth = float(ratio)

        prelaunch_velocity = 0.0
        first_pool = session.scalar(
            select(func.min(models.Pool.created_at_source))
            .join(models.Pair, models.Pair.pool_id == models.Pool.id)
            .where(models.Pair.base_asset_id == asset.id)
        )
        cutoff = first_pool or decision_ts
        if first_pool is not None:
            prelaunch_velocity = float(
                session.scalar(
                    select(func.count())
                    .select_from(models.SocialMention)
                    .where(
                        symbol_filter,
                        models.SocialMention.observed_at <= decision_ts,
                        models.SocialMention.ts < cutoff,
                    )
                )
                or 0
            )
        return growth, channel_diversity, prelaunch_velocity

    def _velocity_features(
        self,
        session: Session,
        asset: models.Asset,
        decision_ts: datetime,
        *,
        source_names: dict[int, str] | None = None,
    ) -> tuple[float | None, float | None, float | None]:
        """Blueprint §6 dev-activity proxies from persisted crawler metrics.

        - ``kol_velocity``: distinct KOL identities (YouTube channel ids +
          Telegram channel handles) mentioning the symbol in the last 24h.
        - ``github_star_velocity``: per-repo stars delta over the trailing
          14d window scaled to stars/day (cumulative stars, so delta == rate),
          max across repos the symbol is mentioned with.
        - ``hf_download_velocity``: same for HuggingFace download counts.

        Star/download history comes from ``RawEvidenceItem`` payloads (each
        crawl is a fresh observation), while ``SocialMention`` rows decide which
        repo URLs are relevant to this asset and which channels are KOLs.
        """
        symbol_filter = (models.SocialMention.asset_id == asset.id) | (
            models.SocialMention.topic.ilike(f"%{asset.symbol}%")
        )
        mentions = session.scalars(
            select(models.SocialMention).where(
                symbol_filter,
                models.SocialMention.observed_at <= decision_ts,
                models.SocialMention.ts > decision_ts - timedelta(days=14),
            )
        ).all()
        if source_names is None:
            source_names = _load_source_names(session)
        source_ids_by_name = {name: source_id for source_id, name in source_names.items()}

        # --- kol_velocity: distinct KOL channels in the trailing 24h ----------
        kol_keys: set[str] = set()
        for mention in mentions:
            if ensure_utc(mention.ts) <= decision_ts - timedelta(hours=24):
                continue
            metrics = mention.metrics_json or {}
            name = source_names.get(mention.source_id)
            key: object = None
            if name == "youtube_rss":
                key = metrics.get("channel_id")
            elif name == "telegram":
                key = metrics.get("channel")
            if key:
                kol_keys.add(str(key))
        kol_velocity = float(len(kol_keys)) if kol_keys else None

        # --- star / download velocity: cumulative-metric delta per repo -------
        def _cumulative_rate(source_name: str, metric_key: str) -> float | None:
            source_id = source_ids_by_name.get(source_name)
            if source_id is None:
                return None
            relevant_urls = {
                str(mention.raw_ref)
                for mention in mentions
                if source_names.get(mention.source_id) == source_name and mention.raw_ref
            }
            if not relevant_urls:
                return None
            evidence = session.scalars(
                select(models.RawEvidenceItem).where(
                    models.RawEvidenceItem.source_id == source_id,
                    models.RawEvidenceItem.observed_at <= decision_ts,
                    models.RawEvidenceItem.observed_at > decision_ts - timedelta(days=14),
                )
            ).all()
            observations: dict[str, list[tuple[datetime, float]]] = {}
            for item in evidence:
                payload = item.payload
                entries = payload.get("items") if isinstance(payload, dict) else None
                if not isinstance(entries, list):
                    continue
                observed = ensure_utc(item.observed_at)
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    url = str(entry.get("url") or "")
                    if url not in relevant_urls:
                        continue
                    metrics = entry.get("metrics")
                    value = metrics.get(metric_key) if isinstance(metrics, dict) else None
                    if not isinstance(value, int | float):
                        continue
                    numeric = float(value)
                    observations.setdefault(url, []).append((observed, numeric))
            best: float | None = None
            for series in observations.values():
                series.sort(key=lambda pair: pair[0])
                if len(series) < 2:
                    continue
                first_ts, first_value = series[0]
                last_ts, last_value = series[-1]
                span_hours = max(1.0, (last_ts - first_ts).total_seconds() / 3600.0)
                rate = (last_value - first_value) * (24.0 / span_hours)
                if best is None or rate > best:
                    best = rate
            return min(100_000.0, best) if best is not None else None

        star_velocity = _cumulative_rate("github_public", "stars")
        download_velocity = _cumulative_rate("huggingface", "downloads")
        return kol_velocity, star_velocity, download_velocity

    def _lifecycle_phase(
        self, session: Session, asset_id: int, decision_ts: datetime
    ) -> float | None:
        row = session.scalar(
            select(models.LifecycleEvent)
            .where(
                models.LifecycleEvent.asset_id == asset_id,
                models.LifecycleEvent.ts <= decision_ts,
                models.LifecycleEvent.observed_at <= decision_ts,
            )
            .order_by(models.LifecycleEvent.ts.desc())
            .limit(1)
        )
        if row is None:
            return None
        try:
            return float(phase_rank(LifecyclePhase(row.phase)))
        except ValueError:
            return None

    def _rpc_pool_health(
        self, session: Session, asset: models.Asset, decision_ts: datetime
    ) -> float:
        """Point-in-time chain RPC health from persisted snapshots in ``[0, 1]``.

        Reads the latest ``RpcPoolSnapshot`` rows observed at or before the
        decision time instead of the live in-process pool — the live pool
        reflects current process memory, which would leak the present into
        historical feature snapshots (and every backtest).  With no snapshot
        yet recorded, a neutral 1.0 is returned (healthy baseline, matching
        the ``_feature_default`` for this name).
        """
        settings = get_settings()
        if not settings.rpc_pool_enabled:
            return 1.0
        chain = session.get(models.Chain, asset.chain_id)
        if chain is None:
            return 1.0
        chain_slug = chain.slug
        latest_ts = session.scalar(
            select(func.max(models.RpcPoolSnapshot.ts)).where(
                models.RpcPoolSnapshot.chain_slug == chain_slug,
                models.RpcPoolSnapshot.ts <= decision_ts,
            )
        )
        if latest_ts is None:
            return 1.0
        states = session.scalars(
            select(models.RpcPoolSnapshot).where(
                models.RpcPoolSnapshot.chain_slug == chain_slug,
                models.RpcPoolSnapshot.ts == latest_ts,
            )
        ).all()
        if not states:
            return 1.0
        return sum(0.0 if row.down else max(0.0, min(1.0, row.health)) for row in states) / len(
            states
        )

    def _forecast_probability(
        self, session: Session, asset_id: int, decision_ts: datetime
    ) -> float | None:
        row = session.scalar(
            select(models.Forecast)
            .where(
                models.Forecast.asset_id == asset_id,
                models.Forecast.decision_ts <= decision_ts,
            )
            .order_by(models.Forecast.decision_ts.desc())
            .limit(1)
        )
        if not row:
            return None
        return float(row.p_collapse_24h)

    def _recidivism_feature(
        self, session: Session, asset_id: int, decision_ts: datetime
    ) -> float | None:
        assessment = session.scalar(
            select(models.FingerprintAssessment)
            .where(
                models.FingerprintAssessment.asset_id == asset_id,
                models.FingerprintAssessment.decision_ts <= decision_ts,
            )
            .order_by(models.FingerprintAssessment.decision_ts.desc())
            .limit(1)
        )
        if not assessment:
            return None
        return float(assessment.recidivism_score)

    def _attention_features(
        self, session: Session, asset: models.Asset, decision_ts: datetime
    ) -> tuple[float | None, float | None]:
        current_count = session.scalar(
            select(func.count())
            .select_from(models.SocialMention)
            .where(
                models.SocialMention.observed_at <= decision_ts,
                models.SocialMention.ts > decision_ts - timedelta(hours=1),
                (models.SocialMention.asset_id == asset.id)
                | (models.SocialMention.topic.ilike(f"%{asset.symbol}%")),
            )
        )
        previous_count = session.scalar(
            select(func.count())
            .select_from(models.SocialMention)
            .where(
                models.SocialMention.observed_at <= decision_ts,
                models.SocialMention.ts <= decision_ts - timedelta(hours=1),
                models.SocialMention.ts > decision_ts - timedelta(hours=2),
                (models.SocialMention.asset_id == asset.id)
                | (models.SocialMention.topic.ilike(f"%{asset.symbol}%")),
            )
        )
        news_count = session.scalar(
            select(func.count())
            .select_from(models.NewsItem)
            .where(
                models.NewsItem.observed_at <= decision_ts,
                models.NewsItem.observed_at > decision_ts - timedelta(hours=24),
                models.NewsItem.title.ilike(f"%{asset.symbol}%"),
            )
        )
        if current_count is None:
            return None, None
        velocity = float(current_count)
        acceleration = (float(current_count) + float(news_count or 0)) / max(
            1.0, float(previous_count or 0)
        )
        return velocity, acceleration


def build_and_persist_features(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    asset_ids: list[int] | None = None,
    feature_source: str = "sql",
) -> dict[int, dict[str, FeatureValue]]:
    """Build and persist features for the given assets at a decision time.

    ``feature_source`` selects the read path:

    - ``"sql"`` (default): read the live normalized tables (market/liquidity
      snapshots, holders, contract flags, narrative, lifecycle, ...) via
      ``FeatureFactory`` — the hot-DB path.
    - ``"lake"``: replay the lake-covered block (market/liquidity series,
      on-chain holder features, and the contract-flag count) entirely from
      the archived Parquet lake via ``LakeFeatureFactory`` (DuckDB) and
      persist it with the same ``upsert_feature`` — no live market,
      liquidity, holder, or contract-flag tables are touched. Backtests use
      this to replay features from the archived evidence without depending
      on hot-DB state.
    """
    if feature_source not in ("sql", "lake"):
        raise ValueError(f"feature_source must be 'sql' or 'lake', got {feature_source!r}")
    decision_ts = ensure_utc(decision_ts or utc_now())
    if feature_source == "lake":
        from features.lake import LakeFeatureFactory

        return LakeFeatureFactory().persist_for_assets(
            session, decision_ts=decision_ts, asset_ids=asset_ids
        )
    return FeatureFactory().persist_for_assets(
        session, decision_ts=decision_ts, asset_ids=asset_ids
    )
