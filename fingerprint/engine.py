from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

import networkx as nx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import (
    AlertState,
    AlertType,
    IgnitionEventType,
    RiskBand,
    WalletRole,
)
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

TOXIC_BANDS = {RiskBand.RED.value, RiskBand.BLACK.value}

ROLE_WEIGHTS: dict[str, float] = {
    WalletRole.DEPLOYER.value: 1.0,
    WalletRole.LP_REMOVER.value: 1.2,
    WalletRole.FUNDER.value: 0.8,
    WalletRole.SNIPER.value: 0.7,
    WalletRole.UNKNOWN.value: 0.5,
}

CONFIDENCE_CAP = 5.0


class FingerprintEngine:
    """Learns wallet syndicates from co-occurrence and scores recidivism per asset.

    ``learn`` builds wallet clusters from wallets that repeatedly appear together
    in the same token's launch set (top holders + deployer). ``assess`` then scores
    how much of a new token's launch wallet set overlaps known clusters, weighted
    by each cluster's toxic history (RED/BLACK risk or on-chain LP withdrawals).
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------ learning

    def learn(self, session: Session, *, decision_ts: datetime | None = None) -> int:
        decision_ts = ensure_utc(decision_ts or utc_now())
        built = 0
        try:
            graph = nx.Graph()
            for wallets in self._actor_sets(session, decision_ts):
                members = sorted(set(wallets))
                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        a, b = members[i], members[j]
                        if graph.has_edge(a, b):
                            graph[a][b]["weight"] += 1
                        else:
                            graph.add_edge(a, b, weight=1)
            edges = [
                (u, v)
                for u, v, data in graph.edges(data=True)
                if int(data.get("weight", 0)) >= self.settings.fingerprint_min_cooccurrence
            ]
            subgraph = nx.Graph()
            subgraph.add_edges_from(edges)
            for component in nx.connected_components(subgraph):
                if len(component) < 2:
                    continue
                if self._cluster_exists(session, component):
                    continue
                roles = self._roles_for_wallets(session, component, decision_ts)
                confidence = self._cluster_confidence(subgraph, component)
                first_seen = (
                    self._earliest_holder_ts(session, component, decision_ts) or decision_ts
                )
                cluster = models.WalletCluster(
                    method_version=self.settings.fingerprint_model_version,
                    confidence=confidence,
                    first_seen_at=first_seen,
                )
                session.add(cluster)
                session.flush()
                for wallet in sorted(component):
                    session.add(
                        models.WalletClusterMember(
                            cluster_id=cluster.id,
                            wallet_address=wallet,
                            confidence=confidence,
                            role=roles.get(wallet, WalletRole.UNKNOWN.value),
                        )
                    )
                built += 1
            record_health(
                session,
                component="fingerprint_learn",
                state="ok",
                message=f"{built} clusters",
            )
        except Exception as exc:  # noqa: BLE001 - preserve exact learning failure.
            log.exception("fingerprint_learn_failed", error=str(exc))
            record_health(
                session,
                component="fingerprint_learn",
                state="red",
                message=str(exc),
                error_count=1,
            )
        return built

    def _actor_sets(self, session: Session, decision_ts: datetime) -> list[set[str]]:
        output: list[set[str]] = []
        for asset in session.scalars(select(models.Asset)).all():
            wallets: set[str] = set()
            latest_ts = session.scalar(
                select(func.max(models.Holder.ts)).where(
                    models.Holder.asset_id == asset.id,
                    models.Holder.ts <= decision_ts,
                    models.Holder.observed_at <= decision_ts,
                )
            )
            if latest_ts:
                holders = session.scalars(
                    select(models.Holder)
                    .where(
                        models.Holder.asset_id == asset.id,
                        models.Holder.ts == latest_ts,
                    )
                    .order_by(models.Holder.balance.desc())
                    .limit(self.settings.fingerprint_learn_top_holders)
                ).all()
                wallets.update(holder.wallet_address for holder in holders)
            deployer = session.scalar(
                select(models.Contract.deployer_wallet)
                .where(
                    models.Contract.asset_id == asset.id,
                    models.Contract.deployer_wallet.is_not(None),
                )
                .limit(1)
            )
            if deployer:
                wallets.add(str(deployer))
            if wallets:
                output.append(wallets)
        return output

    def _cluster_exists(self, session: Session, members: set[str]) -> bool:
        clusters = session.scalars(
            select(models.WalletCluster).where(
                models.WalletCluster.method_version == self.settings.fingerprint_model_version
            )
        ).all()
        for cluster in clusters:
            existing = set(
                session.scalars(
                    select(models.WalletClusterMember.wallet_address).where(
                        models.WalletClusterMember.cluster_id == cluster.id
                    )
                ).all()
            )
            if existing == members:
                return True
        return False

    def _roles_for_wallets(
        self, session: Session, wallets: set[str], decision_ts: datetime
    ) -> dict[str, str]:
        roles: dict[str, str] = {}
        sorted_wallets = sorted(wallets)
        for (wallet,) in session.execute(
            select(models.Contract.deployer_wallet).where(
                models.Contract.deployer_wallet.in_(sorted_wallets),
                models.Contract.deployer_wallet.is_not(None),
            )
        ).all():
            roles[str(wallet)] = WalletRole.DEPLOYER.value

        withdrawn_asset_ids = set(
            session.scalars(
                select(models.IgnitionEvent.asset_id).where(
                    models.IgnitionEvent.event_type
                    == IgnitionEventType.LIQUIDITY_WITHDRAWAL.value,
                    models.IgnitionEvent.observed_at <= decision_ts,
                )
            ).all()
        )
        sniper_asset_ids = set(
            session.scalars(
                select(models.IgnitionEvent.asset_id).where(
                    models.IgnitionEvent.event_type == IgnitionEventType.SNIPER_BURST.value,
                    models.IgnitionEvent.observed_at <= decision_ts,
                )
            ).all()
        )
        sniper_counts: defaultdict[str, int] = defaultdict(int)
        for wallet, asset_id in session.execute(
            select(models.Holder.wallet_address, models.Holder.asset_id).where(
                models.Holder.wallet_address.in_(sorted_wallets),
                models.Holder.ts <= decision_ts,
                models.Holder.observed_at <= decision_ts,
            )
        ).all():
            wallet_key = str(wallet)
            if asset_id in withdrawn_asset_ids and wallet_key not in roles:
                roles[wallet_key] = WalletRole.LP_REMOVER.value
            if asset_id in sniper_asset_ids:
                sniper_counts[wallet_key] += 1
        for wallet, count in sniper_counts.items():
            if count >= 2 and wallet not in roles:
                roles[wallet] = WalletRole.SNIPER.value
        for wallet in wallets:
            roles.setdefault(wallet, WalletRole.UNKNOWN.value)
        return roles

    def _cluster_confidence(self, graph: nx.Graph, component: set[str]) -> float:
        weights = [
            float(graph[u][v].get("weight", 0))
            for u in component
            for v in component
            if u < v and graph.has_edge(u, v)
        ]
        if not weights:
            return 0.5
        return max(0.1, min(1.0, (sum(weights) / len(weights)) / CONFIDENCE_CAP))

    def _earliest_holder_ts(
        self, session: Session, wallets: set[str], decision_ts: datetime
    ) -> datetime | None:
        return session.scalar(
            select(func.min(models.Holder.ts)).where(
                models.Holder.wallet_address.in_(sorted(wallets)),
                models.Holder.ts <= decision_ts,
                models.Holder.observed_at <= decision_ts,
            )
        )

    # ---------------------------------------------------------------- assessment

    def assess(
        self,
        session: Session,
        *,
        decision_ts: datetime | None = None,
        asset_ids: list[int] | None = None,
    ) -> list[models.FingerprintAssessment]:
        decision_ts = ensure_utc(decision_ts or utc_now())
        output: list[models.FingerprintAssessment] = []
        try:
            stmt = select(models.Asset)
            if asset_ids is not None:
                stmt = stmt.where(models.Asset.id.in_(asset_ids))
            for asset in session.scalars(stmt).all():
                assessment = self._assess_asset(session, asset, decision_ts)
                if assessment:
                    output.append(assessment)
            record_health(
                session,
                component="fingerprint",
                state="ok",
                message=f"{len(output)} assessments",
            )
        except Exception as exc:  # noqa: BLE001 - preserve exact assessment failure.
            log.exception("fingerprint_assess_failed", error=str(exc))
            record_health(
                session,
                component="fingerprint",
                state="red",
                message=str(exc),
                error_count=1,
            )
        return output

    def _assess_asset(
        self, session: Session, asset: models.Asset, decision_ts: datetime
    ) -> models.FingerprintAssessment | None:
        wallets = self._asset_wallets(session, asset.id, decision_ts)
        if not wallets:
            return None
        memberships = self._cluster_memberships(session, wallets)
        matched: list[dict[str, Any]] = []
        matched_wallets: set[str] = set()
        matched_roles: set[str] = set()
        for cluster_id, info in memberships.items():
            member_wallets = info["wallets"] & wallets
            if not member_wallets:
                continue
            role = max(
                (
                    ROLE_WEIGHTS.get(
                        info["roles"].get(wallet), ROLE_WEIGHTS[WalletRole.UNKNOWN.value]
                    )
                    for wallet in member_wallets
                ),
                default=ROLE_WEIGHTS[WalletRole.UNKNOWN.value],
            )
            role_label = max(
                member_wallets,
                key=lambda wallet: ROLE_WEIGHTS.get(
                    info["roles"].get(wallet, WalletRole.UNKNOWN.value), 0.0
                ),
            )
            role_name = info["roles"].get(role_label, WalletRole.UNKNOWN.value)
            toxic_rate, toxic_assets, total_assets = self._cluster_toxic_history(
                session, cluster_id, decision_ts, exclude_asset_id=asset.id
            )
            matched.append(
                {
                    "cluster_id": cluster_id,
                    "size": len(info["wallets"]),
                    "matched_wallets": sorted(member_wallets),
                    "role": role_name,
                    "role_weight": round(role, 4),
                    "toxic_rate": round(toxic_rate, 4),
                    "toxic_assets": toxic_assets,
                    "total_assets": total_assets,
                }
            )
            matched_wallets.update(member_wallets)
            matched_roles.update(
                info["roles"].get(wallet, WalletRole.UNKNOWN.value) for wallet in member_wallets
            )

        raw = sum(
            entry["toxic_rate"]
            * ROLE_WEIGHTS.get(entry["role"], ROLE_WEIGHTS[WalletRole.UNKNOWN.value])
            * math.log2(1.0 + entry["size"])
            for entry in matched
        )
        recidivism = 100.0 * (1.0 - math.exp(-0.35 * raw))
        if len(matched) >= 2:
            recidivism = min(100.0, recidivism + 8.0)
        recidivism = max(0.0, min(100.0, recidivism))

        assessment = self._upsert_assessment(
            session,
            asset_id=asset.id,
            decision_ts=decision_ts,
            recidivism_score=round(recidivism, 4),
            matched_cluster_count=len(matched),
            matched_wallet_count=len(matched_wallets),
            matched_roles=sorted(matched_roles),
            matched_clusters=matched,
        )
        if recidivism >= self.settings.recidivism_alert_threshold:
            self._maybe_recidivism_alert(session, asset, assessment)
        return assessment

    def _asset_wallets(
        self, session: Session, asset_id: int, decision_ts: datetime
    ) -> set[str]:
        wallets: set[str] = set()
        latest_ts = session.scalar(
            select(func.max(models.Holder.ts)).where(
                models.Holder.asset_id == asset_id,
                models.Holder.ts <= decision_ts,
                models.Holder.observed_at <= decision_ts,
            )
        )
        if latest_ts:
            holders = session.scalars(
                select(models.Holder)
                .where(
                    models.Holder.asset_id == asset_id,
                    models.Holder.ts == latest_ts,
                )
                .order_by(models.Holder.balance.desc())
                .limit(self.settings.fingerprint_top_holders)
            ).all()
            wallets.update(holder.wallet_address for holder in holders)
        deployer = session.scalar(
            select(models.Contract.deployer_wallet)
            .where(
                models.Contract.asset_id == asset_id,
                models.Contract.deployer_wallet.is_not(None),
            )
            .limit(1)
        )
        if deployer:
            wallets.add(str(deployer))
        return wallets

    def _cluster_memberships(
        self, session: Session, wallets: set[str]
    ) -> dict[int, dict[str, Any]]:
        output: dict[int, dict[str, Any]] = {}
        rows = session.execute(
            select(
                models.WalletClusterMember.cluster_id,
                models.WalletClusterMember.wallet_address,
                models.WalletClusterMember.role,
            ).where(models.WalletClusterMember.wallet_address.in_(sorted(wallets)))
        ).all()
        for cluster_id, wallet, role in rows:
            info = output.setdefault(
                int(cluster_id),
                {"wallets": set(), "roles": {}},
            )
            info["wallets"].add(str(wallet))
            info["roles"][str(wallet)] = str(role or WalletRole.UNKNOWN.value)
        return output

    def _cluster_toxic_history(
        self,
        session: Session,
        cluster_id: int,
        decision_ts: datetime,
        exclude_asset_id: int | None = None,
    ) -> tuple[float, int, int]:
        wallets = set(
            session.scalars(
                select(models.WalletClusterMember.wallet_address).where(
                    models.WalletClusterMember.cluster_id == cluster_id
                )
            ).all()
        )
        if not wallets:
            return 0.0, 0, 0
        asset_ids = set(
            session.scalars(
                select(models.Holder.asset_id)
                .where(
                    models.Holder.wallet_address.in_(sorted(wallets)),
                    models.Holder.ts <= decision_ts,
                    models.Holder.observed_at <= decision_ts,
                )
                .distinct()
            ).all()
        )
        for contract_asset_id in session.scalars(
            select(models.Contract.asset_id).where(
                models.Contract.deployer_wallet.in_(sorted(wallets)),
                models.Contract.deployer_wallet.is_not(None),
            )
        ).all():
            if contract_asset_id is not None:
                asset_ids.add(contract_asset_id)
        if exclude_asset_id is not None:
            asset_ids.discard(exclude_asset_id)
        if not asset_ids:
            return 0.0, 0, 0
        toxic = 0
        for asset_id in asset_ids:
            score = session.scalar(
                select(models.Score)
                .where(
                    models.Score.asset_id == asset_id,
                    models.Score.decision_ts <= decision_ts,
                )
                .order_by(models.Score.decision_ts.desc())
                .limit(1)
            )
            withdrawal_count = session.scalar(
                select(func.count())
                .select_from(models.IgnitionEvent)
                .where(
                    models.IgnitionEvent.asset_id == asset_id,
                    models.IgnitionEvent.event_type
                    == IgnitionEventType.LIQUIDITY_WITHDRAWAL.value,
                    models.IgnitionEvent.observed_at <= decision_ts,
                )
            ) or 0
            if (score is not None and score.risk_band in TOXIC_BANDS) or withdrawal_count > 0:
                toxic += 1
        return toxic / len(asset_ids), toxic, len(asset_ids)

    def _upsert_assessment(
        self,
        session: Session,
        *,
        asset_id: int,
        decision_ts: datetime,
        recidivism_score: float,
        matched_cluster_count: int,
        matched_wallet_count: int,
        matched_roles: list[str],
        matched_clusters: list[dict[str, Any]],
    ) -> models.FingerprintAssessment:
        row = session.scalar(
            select(models.FingerprintAssessment).where(
                models.FingerprintAssessment.asset_id == asset_id,
                models.FingerprintAssessment.decision_ts == decision_ts,
                models.FingerprintAssessment.model_version
                == self.settings.fingerprint_model_version,
            )
        )
        if row:
            row.recidivism_score = recidivism_score
            row.matched_cluster_count = matched_cluster_count
            row.matched_wallet_count = matched_wallet_count
            row.matched_roles = matched_roles
            row.matched_clusters = matched_clusters
            row.observed_at = utc_now()
            return row
        row = models.FingerprintAssessment(
            asset_id=asset_id,
            decision_ts=decision_ts,
            observed_at=utc_now(),
            recidivism_score=recidivism_score,
            matched_cluster_count=matched_cluster_count,
            matched_wallet_count=matched_wallet_count,
            matched_roles=matched_roles,
            matched_clusters=matched_clusters,
            model_version=self.settings.fingerprint_model_version,
        )
        session.add(row)
        session.flush()
        return row

    def _maybe_recidivism_alert(
        self, session: Session, asset: models.Asset, assessment: models.FingerprintAssessment
    ) -> None:
        ref = f"fingerprint:{assessment.id}"
        existing = session.scalar(
            select(models.Alert).where(
                models.Alert.asset_id == asset.id,
                models.Alert.alert_type == AlertType.SYNDICATE_RECIDIVISM.value,
                models.Alert.score_snapshot_ref == ref,
            )
        )
        if existing:
            return
        from ops.alert_quality import alert_generation_allowed
        if not alert_generation_allowed(
            session, AlertType.SYNDICATE_RECIDIVISM.value, self.settings
        ):
            return
        session.add(
            models.Alert(
                asset_id=asset.id,
                alert_type=AlertType.SYNDICATE_RECIDIVISM.value,
                threshold_version=self.settings.fingerprint_model_version,
                score_snapshot_ref=ref,
                state=AlertState.OPEN.value,
                message=(
                    f"Syndicate recidivism {assessment.recidivism_score:.0f}/100 for "
                    f"{asset.symbol}: launch wallets match {assessment.matched_cluster_count} "
                    f"known cluster(s) with toxic history."
                ),
            )
        )
