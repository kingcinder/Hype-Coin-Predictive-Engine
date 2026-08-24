from __future__ import annotations

from datetime import UTC, datetime, timedelta

import respx
from httpx import Response
from sqlalchemy import func, select

from ingestion.rpc_pool import RpcPoolAlert
from ops.notifier import NtfyNotifier
from storage import models
from tests.conftest import seed_market_asset

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
NTFY_URL = "https://ntfy.sh/serpent-test"


def _seed_alert(
    session, *, asset_id: int, alert_type: str, created_at: datetime
) -> models.Alert:
    alert = models.Alert(
        asset_id=asset_id,
        alert_type=alert_type,
        threshold_version="test",
        state="open",
        message=f"{alert_type} fired",
    )
    session.add(alert)
    session.flush()
    alert.created_at = created_at
    return alert


def _enable_ntfy(notifier: NtfyNotifier, *, digest: bool = False) -> None:
    notifier.settings.ntfy_enabled = True
    notifier.settings.ntfy_topic = "serpent-test"
    notifier.settings.ntfy_daily_digest_enabled = digest


def test_acked_alerts_are_not_pushed(session) -> None:
    """ACKing an alert removes it from the notifier's open set: repeat pushes
    are suppressed while other open alerts still flush."""
    asset = seed_market_asset(session)
    open_alert = _seed_alert(
        session, asset_id=asset.id, alert_type="ignition_detected", created_at=NOW
    )
    acked = _seed_alert(
        session, asset_id=asset.id, alert_type="syndicate_recidivism", created_at=NOW
    )
    acked.state = "acked"
    acked.acked_at = NOW
    acked.ack_quality = "noise"
    session.commit()
    assert open_alert.id

    notifier = NtfyNotifier()
    _enable_ntfy(notifier)
    titles: list[str] = []

    def handler(request):
        titles.append(request.headers.get("title", ""))
        return Response(200, content=b"ok")

    with respx.mock:
        respx.post(NTFY_URL).mock(side_effect=handler)
        result = notifier.flush(session, decision_ts=NOW)
        session.commit()

    assert result["sent"] == 1
    assert len(titles) == 1
    assert "Ignition Detected" in titles[0]
    assert not any("Syndicate Recidivism" in title for title in titles)


def test_notifier_pushes_each_type_once(session) -> None:
    asset = seed_market_asset(session)
    for alert_type in (
        "ignition_detected",
        "liquidity_withdrawal_warning",
        "syndicate_recidivism",
        "lifecycle_transition",
    ):
        _seed_alert(session, asset_id=asset.id, alert_type=alert_type, created_at=NOW)
    session.commit()

    notifier = NtfyNotifier()
    _enable_ntfy(notifier)
    sent_payloads: list[dict] = []

    def handler(request):
        sent_payloads.append(
            {
                "body": request.content.decode(),
                "title": request.headers.get("title"),
                "priority": request.headers.get("priority"),
                "tags": request.headers.get("tags"),
            }
        )
        return Response(200, content=b"ok")

    with respx.mock:
        respx.post(NTFY_URL).mock(side_effect=handler)
        result = notifier.flush(session, decision_ts=NOW)
        session.commit()
        assert result["sent"] == 4
        assert result["errors"] == 0
        assert len(sent_payloads) == 4
        titles = {item["title"] for item in sent_payloads}
        assert any("Ignition Detected" in title for title in titles)
        assert any("Liquidity Withdrawal Warning" in title for title in titles)
        assert any("Syndicate Recidivism" in title for title in titles)
        assert any("Lifecycle Transition" in title for title in titles)
        lifecycle = next(
            item for item in sent_payloads if "Lifecycle Transition" in item["title"]
        )
        assert lifecycle["priority"] == "5"
        assert lifecycle["tags"] == "collision"

    # idempotent: already-notified alerts are not re-pushed
    with respx.mock:
        respx.post(NTFY_URL).mock(return_value=Response(200, content=b"ok"))
        result = notifier.flush(session, decision_ts=NOW)
        session.commit()
        assert result["sent"] == 0

    notified = session.scalars(
        select(models.Alert).where(models.Alert.notified_at.is_not(None))
    ).all()
    assert len(notified) == 4


def test_notifier_retries_failed_push(session) -> None:
    asset = seed_market_asset(session)
    _seed_alert(
        session, asset_id=asset.id, alert_type="ignition_detected", created_at=NOW
    )
    session.commit()
    notifier = NtfyNotifier()
    _enable_ntfy(notifier)

    with respx.mock:
        respx.post(NTFY_URL).mock(return_value=Response(500))
        result = notifier.flush(session, decision_ts=NOW)
        session.commit()
        assert result["errors"] == 1
        assert result["sent"] == 0
        alert = session.scalar(select(models.Alert))
        assert alert.notified_at is None  # retried on the next scan

    with respx.mock:
        respx.post(NTFY_URL).mock(return_value=Response(200, content=b"ok"))
        result = notifier.flush(session, decision_ts=NOW)
        session.commit()
        assert result["sent"] == 1
        assert session.scalar(select(models.Alert)).notified_at is not None


def test_notifier_skips_stale_backlog(session) -> None:
    asset = seed_market_asset(session)
    _seed_alert(
        session,
        asset_id=asset.id,
        alert_type="syndicate_recidivism",
        created_at=NOW - timedelta(hours=48),
    )
    session.commit()
    notifier = NtfyNotifier()
    _enable_ntfy(notifier)
    with respx.mock:
        respx.post(NTFY_URL).mock(return_value=Response(200, content=b"ok"))
        result = notifier.flush(session, decision_ts=NOW)
        session.commit()
        assert result["sent"] == 0
        assert result["pending"] == 0
        assert session.scalar(select(models.Alert)).notified_at is None


def test_notifier_pushes_rpc_pool_alert_without_an_asset(session) -> None:
    notifier = NtfyNotifier()
    _enable_ntfy(notifier)
    event = RpcPoolAlert(
        chain_slug="ethereum",
        kind="endpoint_down_cooldown",
        url="https://eth.example.com",
        healthy_endpoints=2,
        total_endpoints=4,
        down_for_seconds=901,
    )
    with respx.mock:
        route = respx.post(NTFY_URL).mock(return_value=Response(200, content=b"ok"))
        assert notifier.send_rpc_pool_event(event) is True
        assert route.calls.last.request.headers["title"] == "[ETHEREUM] RPC Endpoint Down"
        assert "15 minutes" in route.calls.last.request.content.decode()


def test_notifier_sends_daily_digest_with_terminal_and_ignition_events(session) -> None:
    asset = seed_market_asset(session)
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    session.add(
        models.LifecycleEvent(
            asset_id=asset.id,
            phase="collapse",
            event_type="phase_transition",
            ts=NOW - timedelta(hours=2),
            observed_at=NOW - timedelta(hours=2),
            confidence=0.9,
            details={
                "one_hour_return_pct": -32.0,
                "withdrawal_events": 1,
                "liquidity_usd": 12_000.0,
            },
        )
    )
    session.add(
        models.IgnitionEvent(
            asset_id=asset.id,
            source_id=source.id,
            event_type="sniper_burst",
            ts=NOW - timedelta(hours=1),
            observed_at=NOW - timedelta(hours=1),
            confidence=0.85,
            details={"buys": 40},
        )
    )
    session.commit()
    notifier = NtfyNotifier()
    _enable_ntfy(notifier, digest=True)
    payloads: list[dict[str, str]] = []

    def handler(request):
        payloads.append(
            {
                "body": request.content.decode(),
                "title": request.headers.get("title", ""),
            }
        )
        return Response(200, content=b"ok")

    with respx.mock:
        route = respx.post(NTFY_URL).mock(side_effect=handler)
        result = notifier.flush(session, decision_ts=NOW)
        session.commit()
        assert route.call_count == 1
        assert result["sent"] == 0
        assert result["digest"]["status"] == "sent"
        assert result["digest"]["terminal"] == 1
        assert result["digest"]["ignitions"] == 1
        assert payloads[0]["title"] == "Serpent Circle - Daily Digest"
        assert "HYPE [COLLAPSE]" in payloads[0]["body"]
        assert "sniper_burst" in payloads[0]["body"]
        assert "one_hour_return_pct=-32.0" in payloads[0]["body"]

    digest = session.scalar(select(models.NotificationDigest))
    assert digest is not None
    assert digest.digest_key == "2026-05-01"
    assert digest.sent_at is not None

    with respx.mock:
        route = respx.post(NTFY_URL).mock(return_value=Response(200, content=b"ok"))
        result = notifier.flush(session, decision_ts=NOW)
        session.commit()
        assert route.call_count == 0
        assert result["digest"]["status"] == "already_sent"


def test_notifier_skipped_when_disabled(session) -> None:
    asset = seed_market_asset(session)
    _seed_alert(session, asset_id=asset.id, alert_type="ignition_detected", created_at=NOW)
    session.commit()
    notifier = NtfyNotifier()
    notifier.settings.ntfy_enabled = False
    notifier.settings.ntfy_topic = ""
    with respx.mock:
        respx.post(NTFY_URL).mock(return_value=Response(200, content=b"ok"))
        result = notifier.flush(session, decision_ts=NOW)
        session.commit()
        assert result["skipped"] is True
        assert session.scalar(select(func.count()).select_from(models.Alert)) == 1
        assert session.scalar(select(models.Alert)).notified_at is None
