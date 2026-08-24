from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.main import app
from common.config import get_settings
from fingerprint.engine import FingerprintEngine
from pump_physics import run_lifecycle
from radar.ignition import IgnitionRadar
from scoring.engine import score_current_assets
from storage import models
from storage.database import get_session
from tests.conftest import seed_market_asset


def test_api_endpoints_return_fixture_data(session) -> None:
    asset = seed_market_asset(session)
    similar_asset = seed_market_asset(
        session,
        address="Token222222222222222222222222222222222222",
        symbol="SIM",
        pair_address="Pair222222222222222222222222222222222222",
    )
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    score_current_assets(
        session, decision_ts=decision_ts, asset_ids=[asset.id, similar_asset.id]
    )
    IgnitionRadar().scan(session, decision_ts=decision_ts)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    session.add(
        models.Holder(
            asset_id=asset.id,
            wallet_address="wallet-api-test",
            source_id=source.id,
            ts=decision_ts,
            observed_at=decision_ts,
            balance=1_000_000,
            pct_supply=0.2,
        )
    )
    session.flush()
    FingerprintEngine().assess(session, decision_ts=decision_ts)
    run_lifecycle(session, decision_ts=decision_ts)
    session.add(
        models.ArchiveManifest(
            object_key="raw-evidence/source=dexscreener/year=2026/month=05/part-0.parquet",
            source_id=source.id,
            partition_year=2026,
            partition_month=5,
            row_count=3,
            byte_size=1024,
            first_observed_at=decision_ts,
            last_observed_at=decision_ts,
        )
    )
    session.add(
        models.Forecast(
            asset_id=asset.id,
            decision_ts=decision_ts,
            observed_at=decision_ts,
            p_ignition_24h=0.4,
            p_collapse_24h=0.2,
            expected_hours_to_peak=8.0,
            expected_hours_to_collapse=16.0,
            calibration_bucket="20-30%",
            calibrated=True,
            details={
                "feature_contributions": {
                    "github_star_velocity": {
                        "value": 12.0,
                        "baseline": 0.0,
                        "missing": False,
                        "p_ignition_delta": 0.08,
                        "p_collapse_delta": -0.02,
                    }
                }
            },
            model_version="test-forecast-v1",
        )
    )
    session.commit()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        assert client.get("/health").status_code == 200
        top = client.get("/scores/top").json()
        assert isinstance(top, list)
        hot = client.get("/tokens/hot", params={"include_black": True}).json()
        assert hot[0]["symbol"] == "HYPE"
        detail = client.get(f"/tokens/{asset.id}").json()
        assert detail["latest_score"]["asset_id"] == asset.id
        risk = client.get(f"/risk/{asset.id}").json()
        assert risk["risk_band"] in {"GREEN", "YELLOW", "ORANGE", "RED", "BLACK"}
        alerts = client.get("/alerts").json()
        assert isinstance(alerts, list)
        backtests = client.get("/backtest/results").json()
        assert isinstance(backtests, list)
        similar = client.get(f"/tokens/{asset.id}/similar").json()
        assert similar[0]["asset_id"] == similar_asset.id
        assert similar[0]["features_compared"] >= 6
        ignitions = client.get("/radar/ignitions").json()
        assert any(item["asset_id"] == asset.id for item in ignitions)
        assert any(item["event_type"] == "sniper_burst" for item in ignitions)
        fingerprints = client.get("/fingerprint/top").json()
        assert any(item["asset_id"] == asset.id for item in fingerprints)
        assert client.get("/fingerprint/top", params={"limit": 5}).status_code == 200
        assert isinstance(client.get("/radar/prelaunch").json(), list)
        forecasts = client.get("/forecasts").json()
        assert isinstance(forecasts, list)
        assert forecasts[0]["details"]["feature_contributions"]["github_star_velocity"][
            "p_ignition_delta"
        ] == 0.08
        assert isinstance(client.get("/narrative/clusters").json(), list)
        assert isinstance(client.get("/catalysts").json(), list)
        manifests = client.get("/archive/manifests").json()
        assert isinstance(manifests, list)
        assert any(item["source_name"] == "dexscreener" for item in manifests)
        assert isinstance(client.get("/retention/runs").json(), list)
        lifecycle = client.get("/lifecycle/current").json()
        assert isinstance(lifecycle, list)
        assert any(item["asset_id"] == asset.id for item in lifecycle)
        assert isinstance(client.get("/lifecycle/events").json(), list)
        detail = client.get(f"/fingerprint/{asset.id}").json()
        assert detail["asset_id"] == asset.id
        pools = client.get("/rpc/pool").json()
        assert {chain["chain"] for chain in pools} == {"solana", "base", "ethereum"}
        assert all(len(chain["endpoints"]) >= 1 for chain in pools)
        assert all(
            {
                "url",
                "health",
                "consecutive_failures",
                "down",
                "last_probe_at",
                "last_probe_ok",
                "probe_count",
                "probe_successes",
                "probe_failures",
                "probe_history",
            }
            <= set(endpoint)
            for chain in pools
            for endpoint in chain["endpoints"]
        )
        velocity = client.get("/features/velocity").json()
        assert any(
            item["asset_id"] == asset.id
            and item["github_star_velocity_missing"] is True
            for item in velocity
        )
    finally:
        app.dependency_overrides.clear()


def test_lifecycle_alerts_api_includes_terminal_evidence(session) -> None:
    asset = seed_market_asset(session)
    transition_ts = datetime(2026, 5, 1, 15, 0, tzinfo=UTC)
    event = models.LifecycleEvent(
        asset_id=asset.id,
        phase="collapse",
        event_type="phase_transition",
        ts=transition_ts,
        observed_at=transition_ts,
        confidence=0.9,
        details={
            "one_hour_return_pct": -31.5,
            "withdrawal_events": 1,
            "liquidity_usd": 12_345.0,
        },
    )
    session.add(event)
    session.flush()
    session.add(
        models.Alert(
            asset_id=asset.id,
            alert_type="lifecycle_transition",
            threshold_version="test-v1",
            score_snapshot_ref=f"lifecycle:{event.id}",
            state="open",
            message="HYPE reached COLLAPSE — terminal lifecycle phase",
        )
    )
    session.commit()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        response = client.get("/lifecycle/alerts")
        assert response.status_code == 200
        rows = response.json()
        assert len(rows) == 1
        assert rows[0]["phase"] == "collapse"
        assert rows[0]["event_id"] == event.id
        assert rows[0]["evidence"]["one_hour_return_pct"] == -31.5
        assert rows[0]["evidence"]["liquidity_usd"] == 12_345.0
    finally:
        app.dependency_overrides.clear()


def test_rpc_pool_api_prefers_persisted_worker_snapshot(session) -> None:
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    session.add(
        models.RpcPoolSnapshot(
            chain_slug="base",
            url="https://persisted-base.example.com",
            ts=decision_ts,
            health=0.25,
            consecutive_failures=3,
            down=True,
            last_probe_at=decision_ts,
            last_probe_ok=False,
            probe_count=4,
            probe_successes=1,
            probe_failures=3,
            probe_history=[{"ts": decision_ts.isoformat(), "ok": False}],
        )
    )
    session.commit()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        base = next(row for row in client.get("/rpc/pool").json() if row["chain"] == "base")
        assert base["state"] == "red"
        endpoint = base["endpoints"][0]
        assert endpoint["url"] == "https://persisted-base.example.com"
        assert endpoint["health"] == 0.25
        assert endpoint["consecutive_failures"] == 3
        assert endpoint["down"] is True
        assert endpoint["last_probe_ok"] is False
        assert endpoint["probe_count"] == 4
        assert endpoint["probe_successes"] == 1
        assert endpoint["probe_failures"] == 3
        assert endpoint["probe_history"][0]["ok"] is False
    finally:
        app.dependency_overrides.clear()


def test_velocity_features_endpoint_reports_live_values(session) -> None:
    from storage.repository import (
        get_or_create_source,
        store_raw_evidence,
        upsert_social_mention,
    )

    asset = seed_market_asset(session)
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    youtube = get_or_create_source(
        session, name="youtube_rss", source_type="social", tier="public_metadata"
    )
    github = get_or_create_source(
        session, name="github_public", source_type="public_metadata", tier="public_metadata"
    )
    repo_url = "https://github.com/example/hype"
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="HYPE video",
        source_id=youtube.id,
        ts=decision_ts - timedelta(hours=1),
        observed_at=decision_ts - timedelta(hours=1),
        metrics_json={"channel_id": "UCkol1"},
        raw_ref="https://youtube.com/watch?v=1",
    )
    upsert_social_mention(
        session,
        asset_id=asset.id,
        topic="HYPE repo",
        source_id=github.id,
        ts=decision_ts,
        observed_at=decision_ts,
        metrics_json={"stars": 130},
        raw_ref=repo_url,
    )
    store_raw_evidence(
        session,
        source=github,
        payload={"items": [{"url": repo_url, "metrics": {"stars": 100}}]},
        observed_at=decision_ts - timedelta(hours=36),
    )
    store_raw_evidence(
        session,
        source=github,
        payload={"items": [{"url": repo_url, "metrics": {"stars": 130}}]},
        observed_at=decision_ts,
    )
    session.commit()
    score_current_assets(session, decision_ts=decision_ts, asset_ids=[asset.id])
    session.commit()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        velocity = client.get("/features/velocity").json()
        row = next(item for item in velocity if item["asset_id"] == asset.id)
        assert row["symbol"] == "HYPE"
        assert row["kol_velocity"] == 1.0
        assert row["kol_velocity_missing"] is False
        assert row["github_star_velocity"] == 20.0  # 30 stars / 36h * 24
        assert row["github_star_velocity_missing"] is False
        assert row["hf_download_velocity_missing"] is True
    finally:
        app.dependency_overrides.clear()


def test_ops_console_api(session) -> None:
    asset = seed_market_asset(session)
    decision_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    session.add(
        models.ScanResult(
            ts=decision_ts,
            duration_sec=45.5,
            pairs=10,
            profiles=5,
            scores=8,
            ignition_events=3,
            fingerprints=2,
            forecasts=4,
            lifecycle=6,
            narrative=7,
            mempool=2,
            lp_removals=1,
            prelaunch=3,
            catalysts=2,
            archive=1,
            ntfy_sent=5,
            rpc_pool_notifications=0,
            rpc_pool_snapshots=9,
            state="ok",
        )
    )
    session.add(
        models.SystemHealth(
            component="notifier",
            ts=decision_ts,
            state="ok",
            message="5 pushed, 0 failed, 0 pending",
            error_count=0,
        )
    )
    session.add(
        models.Alert(
            asset_id=asset.id,
            alert_type="ignition_detected",
            threshold_version="test-v1",
            state="open",
            message="Test ignition alert",
            notified_at=decision_ts,
        )
    )
    session.commit()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        response = client.get("/ops/console")
        assert response.status_code == 200
        data = response.json()
        assert "last_scan" in data
        assert "notifier_health" in data
        assert "recent_alerts" in data
        assert data["last_scan"]["state"] == "ok"
        assert data["last_scan"]["pairs"] == 10
        assert data["last_scan"]["profiles"] == 5
        assert data["last_scan"]["scores"] == 8
        assert data["last_scan"]["duration_sec"] == 45.5
        assert data["notifier_health"]["state"] == "ok"
        assert data["notifier_health"]["message"] == "5 pushed, 0 failed, 0 pending"
        assert len(data["recent_alerts"]) == 1
        assert data["recent_alerts"][0]["alert_type"] == "ignition_detected"
    finally:
        app.dependency_overrides.clear()


def test_alert_ack_path_and_quality_ledger(session) -> None:
    """ACKing an open alert suppresses repeat pushes and feeds the
    signal-quality ledger of operator feedback."""
    asset = seed_market_asset(session)
    alert = models.Alert(
        asset_id=asset.id,
        alert_type="ignition_detected",
        threshold_version="test-v1",
        state="open",
        message="Test ignition alert",
    )
    session.add(alert)
    session.commit()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        # ACK with a quality rating.
        response = client.post(f"/alerts/{alert.id}/ack", json={"quality": "useful"})
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "acked"
        assert body["ack_quality"] == "useful"
        assert body["acked_at"] is not None
        # Re-acking updates the rating.
        response = client.post(f"/alerts/{alert.id}/ack", json={"quality": "noise"})
        assert response.status_code == 200
        assert response.json()["ack_quality"] == "noise"
        # Invalid quality and missing alerts are rejected.
        assert (
            client.post(f"/alerts/{alert.id}/ack", json={"quality": "meh"}).status_code
            == 422
        )
        assert client.post("/alerts/999999/ack", json={}).status_code == 404
        # The ledger reflects operator feedback.
        ledger = client.get("/alerts/quality").json()
        assert ledger["total_acked"] == 1
        assert ledger["noise"] == 1
        assert ledger["useful"] == 0
        assert ledger["useful_rate"] == 0.0
        assert ledger["recent"][0]["ack_quality"] == "noise"
        # The alerts list exposes the ack state.
        rows = client.get("/alerts").json()
        assert rows[0]["acked_at"] is not None
        assert rows[0]["ack_quality"] == "noise"
    finally:
        app.dependency_overrides.clear()


def test_retention_growth_api_projects_disk_full_horizon(session, monkeypatch) -> None:
    decision_ts = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    for index, (days, size) in enumerate(
        [(0, 1_000), (1, 2_000), (2, 4_000)]
    ):
        session.add(
            models.RetentionRun(
                ts=decision_ts + timedelta(days=days),
                partitions=index + 1,
                archived_rows=index + 1,
                byte_size=size,
                compacted=1,
                pruned=0,
                growth_bytes=0,
                growth_pct=None,
                duration_sec=1.0,
            )
        )
    session.commit()

    # Tight capacity cap so the growth rate produces a finite horizon.
    monkeypatch.setenv("ARCHIVE_LAKE_MAX_BYTES", "10000")
    get_settings.cache_clear()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        response = client.get("/retention/growth")
        assert response.status_code == 200
        data = response.json()
        assert len(data["runs"]) == 3
        assert data["max_bytes"] == 10_000
        assert data["growth_rate_bytes_per_hour"] > 0
        assert data["projected_full_at"] is not None
        assert data["days_to_full"] > 0
        assert data["pct_full"] > 0
        # Retention history is returned newest-first like /retention/runs.
        assert data["runs"][0]["byte_size"] == 4_000
        assert data["runs"][-1]["byte_size"] == 1_000
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
