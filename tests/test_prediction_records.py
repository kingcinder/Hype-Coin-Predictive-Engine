from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from common.config import get_settings
from storage import models, repository
from storage.repository import _config_fingerprint_input, record_prediction
from tests.conftest import seed_market_asset


def test_record_prediction_inserts_row(session) -> None:
    """Calling record_prediction inserts one row with a 64-char config_sha256."""
    asset = seed_market_asset(session)
    row = record_prediction(
        session,
        asset_id=asset.id,
        scored_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        evidence_ts=datetime(2026, 5, 1, 11, 0, tzinfo=UTC),
        risk_band="YELLOW",
        risk_score=50.0,
        confidence=85.0,
        confidence_source="fallback",
        missing_features={"holder_count": 1.0},
    )
    assert isinstance(row, models.PredictionRecord)
    assert len(row.code_sha) == 40
    assert len(row.config_sha256) == 64
    assert row.config_sha256 == hashlib.sha256(_config_fingerprint_input().encode()).hexdigest()
    assert session.get(models.PredictionRecord, row.id) is not None


def test_no_update_or_delete_functions() -> None:
    names = [n for n in dir(repository) if n.startswith(("update_prediction", "delete_prediction"))]
    assert names == [], f"PredictionRecord must be append-only; found {names}"


def test_secrets_excluded_from_hash(session, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "engine_api_token", "SECRETVALUE", raising=False)
    payload = _config_fingerprint_input()
    assert "SECRETVALUE" not in payload
