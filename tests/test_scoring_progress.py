"""Bounded progress logging for the post-forecast scoring phase."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from common.enums import RiskBand
from features import factory as factory_mod
from features.factory import FeatureFactory
from scoring import engine as engine_mod
from scoring.engine import ScoringEngine
from scoring.formulas import ScoreResult
from storage import models
from tests.conftest import seed_market_asset


class _LogRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))


def _result() -> ScoreResult:
    return ScoreResult(
        hype=50.0,
        ethos=50.0,
        risk=25.0,
        liquidity_access=50.0,
        manipulation=0.0,
        confidence=50.0,
        uncertainty=0.0,
        catalyst=0.0,
        exit_risk=0.0,
        research_priority=10.0,
        risk_band=RiskBand.GREEN,
        drivers={},
        risk_reasons=[],
        missing_features=[],
    )


def test_scoring_loop_logs_bounded_progress_and_slow_asset(monkeypatch) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(engine_mod, "log", recorder)
    monkeypatch.setattr(engine_mod, "_SCORING_PROGRESS_EVERY", 2)
    monkeypatch.setattr(engine_mod, "_SCORING_SLOW_ASSET_SECONDS", -1.0)
    monkeypatch.setattr(engine_mod, "get_settings", lambda: SimpleNamespace(llm_enabled=False))
    monkeypatch.setattr(
        engine_mod,
        "build_and_persist_features",
        lambda session, **kwargs: {1: {}, 2: {}, 3: {}},
    )
    monkeypatch.setattr(engine_mod, "mask_unreliable_forecast", lambda session, raw: (raw, False))
    monkeypatch.setattr(engine_mod, "compute_scores", lambda *args, **kwargs: _result())
    monkeypatch.setattr(
        engine_mod.ensemble_engine,
        "blend",
        lambda **kwargs: SimpleNamespace(
            hype=50.0, risk=25.0, confidence=50.0, research_priority=10.0
        ),
    )

    def _score(self, session, *, asset_id, decision_ts, result):
        return SimpleNamespace(id=asset_id, asset_id=asset_id)

    monkeypatch.setattr(ScoringEngine, "_rpc_pool_uncertainty", lambda self, session, asset_id: 0.0)
    monkeypatch.setattr(ScoringEngine, "_upsert_score", _score)
    monkeypatch.setattr(ScoringEngine, "_upsert_explanation", lambda *args, **kwargs: None)
    monkeypatch.setattr(ScoringEngine, "_changed_features", lambda *args, **kwargs: {})
    monkeypatch.setattr(ScoringEngine, "_maybe_create_alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(ScoringEngine, "_record_risk_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(ScoringEngine, "_build_price_updates", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine_mod.llm_calibrator, "calibrate", lambda session: None)

    commits: list[int] = []
    session = SimpleNamespace(commit=lambda: commits.append(1))
    scores = ScoringEngine().score_assets(
        session, decision_ts=datetime(2026, 11, 17, 15, 59, tzinfo=UTC)
    )

    assert len(scores) == 3
    assert len(commits) == 3

    progress = [kw for event, kw in recorder.calls if event == "scoring_progress"]
    starts = [kw for kw in progress if kw["phase"] == "asset_start"]
    completions = [kw for kw in progress if kw["phase"] == "asset_complete"]
    assert [kw["asset_id"] for kw in starts] == [1, 2, 3]
    assert [kw["processed"] for kw in completions] == [2, 3]
    assert completions[-1]["total"] == 3
    assert completions[-1]["rate_per_sec"] >= 0

    slow = [kw for event, kw in recorder.calls if event == "scoring_slow_asset"]
    assert [kw["asset_id"] for kw in slow] == [1, 2, 3]
    assert all(kw["elapsed_sec"] >= 0 for kw in slow)


def test_feature_build_logs_bounded_progress_and_slow_asset(session, monkeypatch) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(factory_mod, "log", recorder)
    monkeypatch.setattr(factory_mod, "_FEATURE_PROGRESS_EVERY", 2)
    monkeypatch.setattr(factory_mod, "_FEATURE_SLOW_ASSET_SECONDS", -1.0)
    monkeypatch.setattr(
        FeatureFactory,
        "build_for_asset",
        lambda self, session, asset, decision_ts, source_names=None: [],
    )
    monkeypatch.setattr(factory_mod, "upsert_features", lambda *args, **kwargs: None)

    asset_a = seed_market_asset(
        session, symbol="ALPHA", address="Alpha11111111111111111111111111111111111111"
    )
    asset_b = seed_market_asset(
        session,
        symbol="BETA",
        address="Beta111111111111111111111111111111111111111",
        pair_address="Pair222222222222222222222222222222222222",
    )
    decision_ts = datetime(2026, 11, 17, 15, 59, tzinfo=UTC)

    output = FeatureFactory().persist_for_assets(
        session, decision_ts=decision_ts, asset_ids=[asset_a.id, asset_b.id]
    )

    assert set(output) == {asset_a.id, asset_b.id}
    progress = [kw for event, kw in recorder.calls if event == "feature_build_progress"]
    starts = [kw for kw in progress if kw["phase"] == "asset_start"]
    completions = [kw for kw in progress if kw["phase"] == "asset_complete"]
    assert [kw["asset_id"] for kw in starts] == [asset_a.id, asset_b.id]
    assert [kw["processed"] for kw in completions] == [2]
    assert completions[0]["total"] == 2

    slow = [kw for event, kw in recorder.calls if event == "feature_build_slow_asset"]
    assert [kw["asset_id"] for kw in slow] == [asset_a.id, asset_b.id]
    assert all(isinstance(kw["elapsed_sec"], float) for kw in slow)


def test_feature_build_returns_persisted_output_unchanged(session, monkeypatch) -> None:
    """Progress logging must not alter the returned feature map."""
    monkeypatch.setattr(factory_mod, "_FEATURE_PROGRESS_EVERY", 1)
    monkeypatch.setattr(
        FeatureFactory,
        "build_for_asset",
        lambda self, session, asset, decision_ts, source_names=None: [
            factory_mod.FeatureValue("five_min_return", 1.5, False)
        ],
    )
    asset = seed_market_asset(session)
    decision_ts = datetime(2026, 11, 17, 15, 59, tzinfo=UTC)

    output = FeatureFactory().persist_for_assets(
        session, decision_ts=decision_ts, asset_ids=[asset.id]
    )

    assert output[asset.id]["five_min_return"].value == 1.5
    persisted = (
        session.query(models.Feature)
        .filter_by(
            asset_id=asset.id,
            decision_ts=decision_ts,
            feature_name="five_min_return",
        )
        .one()
    )
    assert persisted.feature_value == 1.5
