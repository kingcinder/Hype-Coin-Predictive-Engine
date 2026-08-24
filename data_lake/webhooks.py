"""Webhook notification system — real-time alerts to configurable endpoints.

Extends the existing ntfy.sh notifier with support for:
- Custom HTTP POST webhooks (any URL)
- Telegram bot integration
- Discord webhook integration
- Event filtering and rate limiting per webhook

Webhooks are registered in the database and dispatched after each scan
when high-signal events are detected.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.logging import get_logger
from common.time import ensure_utc, utc_now
from storage import models

log = get_logger(__name__)

# Default event types that trigger webhooks
DEFAULT_EVENT_TYPES = [
    "ignition_detected",
    "liquidity_withdrawal_warning",
    "syndicate_recidivism",
    "lifecycle_transition",
    "high_signal_scan",
]


@dataclass(frozen=True)
class WebhookDispatchResult:
    """Result of dispatching a single webhook."""

    webhook_id: int
    url: str
    success: bool
    status_code: int | None = None
    error: str | None = None
    duration_ms: float = 0.0


def register_webhook(
    session: Session,
    *,
    url: str,
    name: str,
    event_types: list[str] | None = None,
    secret: str | None = None,
    enabled: bool = True,
    cooldown_seconds: int = 300,
    chain_filter: str | None = None,
    min_signal_score: float = 0.0,
) -> models.WebhookConfig:
    """Register a new webhook endpoint."""
    event_types = event_types or DEFAULT_EVENT_TYPES
    webhook = models.WebhookConfig(
        url=url,
        name=name,
        secret=secret,
        event_types=event_types,
        enabled=enabled,
        cooldown_seconds=cooldown_seconds,
        chain_filter=chain_filter,
        min_signal_score=min_signal_score,
        last_dispatched_at=None,
    )
    session.add(webhook)
    session.flush()
    return webhook


def list_webhooks(session: Session) -> list[models.WebhookConfig]:
    """List all registered webhooks."""
    return list(
        session.scalars(
            select(models.WebhookConfig).order_by(models.WebhookConfig.created_at.desc())
        ).all()
    )


def delete_webhook(session: Session, webhook_id: int) -> bool:
    """Delete a webhook by ID."""
    webhook = session.get(models.WebhookConfig, webhook_id)
    if not webhook:
        return False
    session.delete(webhook)
    session.flush()
    return True


def build_payload(
    event_type: str,
    alert: models.Alert | None,
    asset: models.Asset | None,
    chain: models.Chain | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON payload for a webhook dispatch."""
    payload: dict[str, Any] = {
        "event_type": event_type,
        "timestamp": utc_now().isoformat(),
        "engine": "serpent-circle",
    }

    if alert:
        payload["alert"] = {
            "id": alert.id,
            "alert_type": alert.alert_type,
            "state": alert.state,
            "message": alert.message,
            "created_at": alert.created_at.isoformat() if alert.created_at else None,
        }

    if asset:
        payload["asset"] = {
            "id": asset.id,
            "symbol": asset.symbol,
            "chain": chain.slug if chain else "unknown",
            "address": asset.address,
        }

    if extra:
        payload.update(extra)

    return payload


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Create HMAC-SHA256 signature for the payload."""
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def should_dispatch(
    session: Session,
    webhook: models.WebhookConfig,
    event_type: str,
) -> bool:
    """Check if a webhook should be dispatched based on cooldown and filters."""
    if not webhook.enabled:
        return False

    # Check event type filter
    if event_type not in (webhook.event_types or []):
        return False

    # Check cooldown
    if webhook.last_dispatched_at is not None:
        elapsed = (utc_now() - ensure_utc(webhook.last_dispatched_at)).total_seconds()
        if elapsed < webhook.cooldown_seconds:
            return False

    return True


def _record_dispatch(
    session: Session,
    webhook: models.WebhookConfig,
    result: WebhookDispatchResult,
) -> None:
    """Record a webhook dispatch result."""
    # Update last_dispatched_at
    webhook.last_dispatched_at = utc_now()

    # Record in webhook_dispatches table
    dispatch = models.WebhookDispatch(
        webhook_config_id=webhook.id,
        event_type="dispatch",
        success=result.success,
        status_code=result.status_code,
        error_message=result.error,
        duration_ms=result.duration_ms,
    )
    session.add(dispatch)
    session.flush()


def dispatch_webhook(
    session: Session,
    webhook: models.WebhookConfig,
    event_type: str,
    payload: dict[str, Any],
) -> WebhookDispatchResult:
    """Dispatch a single webhook with signing and error handling."""
    start = time.monotonic()
    try:
        payload_bytes = json.dumps(payload, default=str).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}

        # Add HMAC signature if secret is set
        if webhook.secret:
            headers["X-Signature-256"] = f"sha256={_sign_payload(payload_bytes, webhook.secret)}"

        # Determine endpoint-specific headers
        parsed = urlparse(webhook.url)
        host = parsed.hostname or ""

        if "api.telegram.org" in host:
            # Telegram Bot API: send as JSON with chat_id
            telegram_payload = {
                "chat_id": webhook.name,  # Use name as chat_id for Telegram
                "text": _format_telegram_message(payload),
                "parse_mode": "HTML",
            }
            payload_bytes = json.dumps(telegram_payload, default=str).encode()
        elif "discord.com" in host and "/api/webhooks/" in webhook.url:
            # Discord webhook: send as JSON with content
            discord_payload = {
                "content": _format_discord_message(payload),
            }
            payload_bytes = json.dumps(discord_payload, default=str).encode()

        with httpx.Client(timeout=10.0) as client:
            response = client.post(webhook.url, content=payload_bytes, headers=headers)
            duration_ms = (time.monotonic() - start) * 1000

            result = WebhookDispatchResult(
                webhook_id=webhook.id,
                url=webhook.url,
                success=response.status_code < 400,
                status_code=response.status_code,
                duration_ms=round(duration_ms, 1),
            )

            _record_dispatch(session, webhook, result)
            return result

    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        result = WebhookDispatchResult(
            webhook_id=webhook.id,
            url=webhook.url,
            success=False,
            error=str(exc),
            duration_ms=round(duration_ms, 1),
        )
        _record_dispatch(session, webhook, result)
        return result


def _format_telegram_message(payload: dict[str, Any]) -> str:
    """Format a payload as a Telegram HTML message."""
    event_type = payload.get("event_type", "unknown")
    asset = payload.get("asset", {})
    alert = payload.get("alert", {})

    lines = ["<b>🐍 Serpent Circle Alert</b>"]
    lines.append(f"<i>Event: {event_type}</i>")

    if asset:
        symbol = asset.get("symbol", "UNKNOWN")
        chain = asset.get("chain", "unknown")
        address = asset.get("address", "")
        lines.append(f"\n<b>{symbol}</b> on {chain}")
        lines.append(f"<code>{address}</code>")

    if alert:
        message = alert.get("message", "")
        if message:
            lines.append(f"\n{message}")

    return "\n".join(lines)


def _format_discord_message(payload: dict[str, Any]) -> str:
    """Format a payload as a Discord message."""
    event_type = payload.get("event_type", "unknown")
    asset = payload.get("asset", {})
    alert = payload.get("alert", {})

    lines = ["🐍 **Serpent Circle Alert**"]
    lines.append(f"*Event: {event_type}*")

    if asset:
        symbol = asset.get("symbol", "UNKNOWN")
        chain = asset.get("chain", "unknown")
        address = asset.get("address", "")
        lines.append(f"\n**{symbol}** on {chain}")
        lines.append(f"`{address}`")

    if alert:
        message = alert.get("message", "")
        if message:
            lines.append(f"\n{message}")

    return "\n".join(lines)


def dispatch_alerts(
    session: Session,
    *,
    event_type: str,
    alert: models.Alert | None = None,
    asset: models.Asset | None = None,
    chain: models.Chain | None = None,
    extra: dict[str, Any] | None = None,
) -> list[WebhookDispatchResult]:
    """Dispatch alerts to all matching webhooks."""
    payload = build_payload(event_type, alert, asset, chain, extra)
    webhooks = list_webhooks(session)

    results: list[WebhookDispatchResult] = []
    for webhook in webhooks:
        if should_dispatch(session, webhook, event_type):
            result = dispatch_webhook(session, webhook, event_type, payload)
            results.append(result)
            if result.success:
                log.info(
                    "webhook_dispatched",
                    webhook_id=webhook.id,
                    event_type=event_type,
                    status_code=result.status_code,
                )
            else:
                log.warning(
                    "webhook_dispatch_failed",
                    webhook_id=webhook.id,
                    event_type=event_type,
                    error=result.error,
                )

    return results


def webhook_dispatch_history(
    session: Session,
    webhook_id: int | None = None,
    limit: int = 50,
) -> list[models.WebhookDispatch]:
    """Get recent webhook dispatch history."""
    stmt = select(models.WebhookDispatch).order_by(
        models.WebhookDispatch.dispatched_at.desc()
    ).limit(limit)
    if webhook_id is not None:
        stmt = stmt.where(models.WebhookDispatch.webhook_config_id == webhook_id)
    return list(session.scalars(stmt).all())
