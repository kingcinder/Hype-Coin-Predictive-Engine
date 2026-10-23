from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import app
from common.config import get_settings
from features.factory import compute_solana_safety_features
from ingestion.solana_safety import (
    collect_asset_safety,
    decode_lp_status,
    decode_mint_account,
    fetch_mint_account,
)
from risk_engine.rules import assess_risk
from storage import models
from storage.database import get_session
from tests.conftest import seed_market_asset


def _mint_info(
    *,
    mint_authority: str | None = None,
    freeze_authority: str | None = None,
    extensions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "data": {
            "parsed": {
                "type": "mint",
                "info": {
                    "mintAuthority": mint_authority,
                    "freezeAuthority": freeze_authority,
                    "supply": "1234500",
                    "decimals": 6,
                    "extensions": extensions or [],
                },
            }
        }
    }


def test_decode_revoked_authorities_and_mint_dimensions() -> None:
    evidence = decode_mint_account(_mint_info())

    assert evidence.mint_authority_active is False
    assert evidence.freeze_authority_active is False
    assert evidence.supply == 1234500
    assert evidence.decimals == 6
    assert evidence.transfer_fee_enabled is False


def test_decode_live_authorities_and_token_2022_extensions() -> None:
    evidence = decode_mint_account(
        _mint_info(
            mint_authority="MintAuthority111",
            freeze_authority="FreezeAuthority111",
            extensions=[
                {"extension": "transferFeeConfig"},
                {"extension": "permanentDelegate", "state": {"delegate": "Delegate111"}},
                {"extension": "nonTransferable"},
                {"extension": "defaultAccountState", "state": {"state": "frozen"}},
            ],
        )
    )

    assert evidence.mint_authority_active is True
    assert evidence.freeze_authority_active is True
    assert evidence.transfer_fee_enabled is True
    assert evidence.permanent_delegate_enabled is True
    assert evidence.non_transferable_enabled is True
    assert evidence.default_account_frozen is True


def test_unsupported_lp_account_layout_is_unknown() -> None:
    assert decode_lp_status({"data": {"program": "unknown", "parsed": {}}}) == "unknown"


@pytest.mark.parametrize(
    ("accounts", "expected"),
    [
        ([{"owner": "holder", "amount": "100"}], "unlocked"),
        ([{"owner": "locker", "amount": "100", "locked": True}], "locked"),
        ([{"owner": "1nc1nerator11111111111111111111111111111111", "amount": "100"}], "burned"),
    ],
)
def test_lp_status_uses_complete_parsed_ownership_fixture(
    accounts: list[dict[str, Any]], expected: str
) -> None:
    account = {
        "data": {
            "parsed": {"info": {"lpMint": "LP111", "lpSupply": "100", "lpTokenAccounts": accounts}}
        }
    }

    assert decode_lp_status(account) == expected


def test_rpc_timeout_is_reported_as_missing() -> None:
    class Pool:
        def pick(self) -> str:
            return "https://rpc.example.invalid"

        def mark_failure(self, _url: str) -> None:
            self.failed = True

    class Http:
        def post_json(self, *_args: Any, **_kwargs: Any) -> Any:
            raise TimeoutError("fixture timeout")

    pool = Pool()
    assert fetch_mint_account("Mint111", pool=pool, http=Http()) is None
    assert pool.failed


def test_safety_snapshot_after_decision_ts_is_rejected(session: Any) -> None:
    decision_ts = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    asset = seed_market_asset(session)
    session.add(
        models.SolanaSafetySnapshot(
            asset_id=asset.id,
            observed_at=decision_ts + timedelta(seconds=1),
            status="ok",
            evidence={"mint_authority_active": False},
        )
    )
    session.flush()

    features = compute_solana_safety_features(session, asset, decision_ts)

    mint_authority = next(row for row in features if row.name == "solana_mint_authority_active")
    assert mint_authority.missing is True


def test_timeout_snapshot_emits_missing_safety_features(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ingestion.solana_safety as safety

    asset = seed_market_asset(session)
    monkeypatch.setattr(safety, "fetch_mint_account", lambda *_args, **_kwargs: None)
    collect_asset_safety(session, asset, observed_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC))

    features = compute_solana_safety_features(
        session, asset, datetime(2026, 10, 23, 12, 1, tzinfo=UTC)
    )
    assert all(row.missing for row in features)


def test_feature_factory_exposes_known_safety_values(session: Any) -> None:
    asset = seed_market_asset(session)
    observed_at = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    session.add(
        models.SolanaSafetySnapshot(
            asset_id=asset.id,
            observed_at=observed_at,
            status="ok",
            evidence={
                "mint_authority_active": True,
                "freeze_authority_active": False,
                "supply": 1000,
                "decimals": 6,
                "transfer_fee_enabled": True,
                "permanent_delegate_enabled": False,
                "non_transferable_enabled": False,
                "default_account_frozen": False,
                "lp_status": "unlocked",
            },
        )
    )
    session.flush()

    features = compute_solana_safety_features(session, asset, observed_at + timedelta(seconds=1))
    by_name = {row.name: row for row in features}
    assert by_name["solana_mint_authority_active"].value == 1.0
    assert by_name["solana_mint_authority_active"].missing is False
    assert by_name["solana_lp_unlocked"].value == 1.0


def test_known_safety_flags_add_documented_risk_points() -> None:
    safe_features = {
        "liquidity_depth": 250_000.0,
        "suspicious_contract_flags": 0.0,
        "top_holder_concentration": 0.1,
        "pair_age_minutes": 10_000.0,
        "spread_estimate": 1.0,
        "buy_sell_ratio": 1.0,
        "holder_count": 500.0,
        "volatility": 10.0,
        "liquidity_withdrawal_signal": 0.0,
        "lp_removal_signal": 0.0,
        "recidivism_score": 0.0,
        "collapse_probability_24h": 0.0,
        "lifecycle_phase": 1.0,
        "solana_mint_authority_active": 0.0,
        "solana_freeze_authority_active": 0.0,
        "solana_transfer_fee_enabled": 0.0,
        "solana_permanent_delegate_enabled": 0.0,
        "solana_default_account_frozen": 0.0,
        "solana_lp_unlocked": 0.0,
    }
    active_mint = {**safe_features, "solana_mint_authority_active": 1.0}

    safe = assess_risk(safe_features)
    risky = assess_risk(active_mint)

    assert risky.score - safe.score == pytest.approx(20.0)
    assert any("mint authority" in reason.lower() for reason in risky.reasons)


def test_token_detail_returns_latest_solana_safety_snapshot(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENGINE_API_TOKEN", "test-api-token-not-a-secret")
    get_settings.cache_clear()
    asset = seed_market_asset(session)
    observed_at = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    session.add(
        models.SolanaSafetySnapshot(
            asset_id=asset.id,
            observed_at=observed_at,
            status="ok",
            evidence={"mint_authority_active": False, "lp_status": "unknown"},
        )
    )
    session.flush()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        response = TestClient(
            app, headers={"Authorization": "Bearer test-api-token-not-a-secret"}
        ).get(f"/tokens/{asset.id}")
    finally:
        app.dependency_overrides.pop(get_session, None)
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["solana_safety"]["status"] == "ok"
    assert response.json()["solana_safety"]["evidence"]["lp_status"] == "unknown"
