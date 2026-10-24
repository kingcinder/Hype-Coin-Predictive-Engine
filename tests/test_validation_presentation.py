from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import app
from common.config import get_settings
from validation.presentation import guard_validation_cells


def test_near_perfect_concordance_is_suppressed_without_leakage_audit() -> None:
    guarded = guard_validation_cells(
        {
            "cells": [{"output": "risk_score", "metric": "concordance", "value": 0.99, "n": 100}],
            "suspicious_results": [],
        }
    )

    assert guarded["leakage_crosscheck_ran"] is False
    cell = guarded["cells"][0]
    assert cell["value"] is None
    assert cell["status"] == "suppressed_missing_crosscheck"


def test_near_perfect_concordance_is_flagged_after_suspicious_crosscheck() -> None:
    guarded = guard_validation_cells(
        {
            "cells": [
                {"output": "risk_score", "metric": "concordance", "value": 0.99, "n": 100},
                {
                    "output": "feature_leakage",
                    "metric": "suspected_features",
                    "value": 1.0,
                    "n": 100,
                },
            ],
            "suspicious_results": [
                {"output": "risk_score", "metric": "concordance", "reason": "near perfect"}
            ],
        }
    )

    assert guarded["leakage_crosscheck_ran"] is True
    assert guarded["cells"][0]["value"] == 0.99
    assert guarded["cells"][0]["status"] == "flagged_suspicious"


def test_validation_api_surfaces_crosschecked_warning(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENGINE_API_TOKEN", "test-api-token-not-a-secret")
    get_settings.cache_clear()
    monkeypatch.setattr(api_main, "VALIDATION_REPORTS_DIR", tmp_path)
    (tmp_path / "validation-test.json").write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-10-23T00:00:00+00:00",
                "git_sha": "test",
                "model_version": "fixture",
                "cells": [
                    {
                        "output": "risk_score",
                        "metric": "concordance",
                        "value": 0.99,
                        "n": 100,
                        "verdict": "better",
                    },
                    {
                        "output": "feature_leakage",
                        "metric": "suspected_features",
                        "value": 1.0,
                        "n": 100,
                        "verdict": "worse",
                    },
                ],
                "suspicious_results": [
                    {"output": "risk_score", "metric": "concordance", "reason": "near perfect"}
                ],
                "notes": [],
            }
        ),
        encoding="utf-8",
    )
    try:
        response = TestClient(
            app, headers={"Authorization": "Bearer test-api-token-not-a-secret"}
        ).get("/validation/latest")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["leakage_crosscheck_ran"] is True
    assert payload["cells"][0]["status"] == "flagged_suspicious"
