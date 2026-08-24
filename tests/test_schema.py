from __future__ import annotations

from storage.database import Base


def test_required_tables_are_registered() -> None:
    required = {
        "chains",
        "assets",
        "contracts",
        "pairs",
        "pools",
        "market_snapshots",
        "liquidity_snapshots",
        "holders",
        "wallet_clusters",
        "contract_flags",
        "social_mentions",
        "news_items",
        "catalysts",
        "features",
        "scores",
        "alerts",
        "labels",
        "system_health",
        "sources",
        "raw_evidence_items",
        "wallet_cluster_members",
        "ingestion_watermarks",
        "score_explanations",
        "backtest_runs",
        "backtest_results",
        "ignition_events",
        "fingerprint_assessments",
        "prelaunch_candidates",
        "forecasts",
        "narrative_clusters",
        "archive_manifests",
        "lifecycle_events",
        "retention_runs",
        "rpc_pool_snapshots",
        "liquidity_removal_events",
    }
    assert required.issubset(Base.metadata.tables.keys())


def test_scores_keep_all_ten_score_channels() -> None:
    columns = set(Base.metadata.tables["scores"].columns.keys())
    assert {
        "hype",
        "ethos",
        "risk",
        "liquidity_access",
        "manipulation",
        "confidence",
        "uncertainty",
        "catalyst",
        "exit_risk",
        "research_priority",
    }.issubset(columns)
