from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.enums import AlertState
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from ingestion.rpc_pool import RpcPoolAlert
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

# alert_type -> (ntfy priority 1..5, emoji tag)
_PUSH_PROFILE: dict[str, tuple[int, str]] = {
    "ignition_detected": (3, "rocket"),
    "liquidity_withdrawal_warning": (5, "skull"),
    "syndicate_recidivism": (4, "police_car"),
    "lifecycle_transition": (5, "collision"),
}

_RPC_PUSH_PROFILE: dict[str, tuple[int, str]] = {
    "zero_healthy_endpoints": (5, "rotating_light"),
    "endpoint_down_cooldown": (4, "warning"),
}


class NtfyNotifier:
    """Pushes t0 alerts to ntfy.sh (free, no account on the public server).

    Only the configured alert types are pushed, only once each: rows are marked
    with ``notified_at`` on success, so a failed push is retried on the next scan
    and a successful push is never duplicated. Old alerts outside the backlog
    window are skipped so re-enabling the notifier cannot dump a stale backlog.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ntfy_enabled and self.settings.ntfy_topic)

    def flush(self, session: Session, *, decision_ts: datetime | None = None) -> dict[str, Any]:
        if not self.enabled:
            return {"skipped": True}
        decision_ts = ensure_utc(decision_ts or utc_now())
        session.flush()  # make event and alert rows created earlier in the scan visible
        backlog_start = decision_ts - timedelta(hours=self.settings.ntfy_backlog_hours)
        rows = session.scalars(
            select(models.Alert).where(
                models.Alert.alert_type.in_(self.settings.ntfy_alert_types),
                models.Alert.notified_at.is_(None),
                models.Alert.state == AlertState.OPEN.value,
                models.Alert.created_at >= backlog_start,
            )
        ).all()
        sent = 0
        errors = 0
        for alert in rows:
            try:
                self._send(session, alert)
                alert.notified_at = utc_now()
                sent += 1
            except Exception as exc:  # noqa: BLE001 - push failure is retried, never fatal.
                errors += 1
                log.warning("ntfy_push_failed", alert_id=alert.id, error=str(exc))
        digest = self._daily_digest(session, decision_ts)
        errors += int(digest.get("errors", 0))
        state = "ok" if not errors else "yellow"
        record_health(
            session,
            component="notifier",
            state=state,
            message=(
                f"{sent} pushed, {errors} failed, {len(rows)} pending; "
                f"daily_digest={digest.get('status', 'unknown')}"
            ),
            error_count=errors,
        )
        return {"sent": sent, "errors": errors, "pending": len(rows), "digest": digest}

    def _daily_digest(self, session: Session, decision_ts: datetime) -> dict[str, Any]:
        """Send one durable digest per UTC day, retrying failed deliveries."""
        if not self.settings.ntfy_daily_digest_enabled:
            return {"status": "disabled", "skipped": True}
        window_start = decision_ts - timedelta(hours=24)
        terminal = list(
            session.scalars(
                select(models.LifecycleEvent)
                .where(
                    models.LifecycleEvent.phase.in_(("collapse", "rugged", "dead")),
                    models.LifecycleEvent.ts >= window_start,
                    models.LifecycleEvent.ts <= decision_ts,
                )
                .order_by(models.LifecycleEvent.ts)
            ).all()
        )
        ignitions = list(
            session.scalars(
                select(models.IgnitionEvent)
                .where(
                    models.IgnitionEvent.ts >= window_start,
                    models.IgnitionEvent.ts <= decision_ts,
                )
                .order_by(models.IgnitionEvent.ts)
            ).all()
        )
        digest_key = decision_ts.date().isoformat()
        digest = session.scalar(
            select(models.NotificationDigest).where(
                models.NotificationDigest.digest_key == digest_key
            )
        )
        if digest is not None and digest.sent_at is not None:
            return {
                "status": "already_sent",
                "skipped": True,
                "terminal": digest.terminal_count,
                "ignitions": digest.ignition_count,
            }
        message = self._format_daily_digest(
            session,
            window_start=window_start,
            window_end=decision_ts,
            terminal=terminal,
            ignitions=ignitions,
        )
        if digest is None:
            digest = models.NotificationDigest(
                digest_key=digest_key,
                window_start=window_start,
                window_end=decision_ts,
                terminal_count=len(terminal),
                ignition_count=len(ignitions),
                message=message,
            )
            session.add(digest)
        else:
            digest.window_start = window_start
            digest.window_end = decision_ts
            digest.terminal_count = len(terminal)
            digest.ignition_count = len(ignitions)
            digest.message = message
        try:
            self._post(
                message,
                {
                    "Title": "Serpent Circle - Daily Digest",
                    "Priority": "3",
                    "Tags": "bar_chart",
                    "Click": f"{self.settings.api_base_url}/alerts",
                },
            )
            digest.sent_at = utc_now()
            status = "sent"
            errors = 0
        except Exception as exc:  # noqa: BLE001 - retain the row so next scan retries.
            log.warning("ntfy_daily_digest_failed", digest_key=digest_key, error=str(exc))
            status = "failed"
            errors = 1
        session.flush()
        return {
            "status": status,
            "sent": 1 if not errors else 0,
            "errors": errors,
            "terminal": len(terminal),
            "ignitions": len(ignitions),
        }

    @staticmethod
    def _format_daily_digest(
        session: Session,
        *,
        window_start: datetime,
        window_end: datetime,
        terminal: list[models.LifecycleEvent],
        ignitions: list[models.IgnitionEvent],
    ) -> str:
        lines = [
            "Serpent Circle daily digest",
            f"Window: {window_start.isoformat()} → {window_end.isoformat()}",
            f"Terminal transitions: {len(terminal)}",
            f"Ignitions: {len(ignitions)}",
        ]
        if terminal:
            lines.append("\nTerminal transitions:")
            for lifecycle_event in terminal:
                asset = session.get(models.Asset, lifecycle_event.asset_id)
                symbol = asset.symbol if asset else "?"
                evidence = ", ".join(
                    f"{key}={value}"
                    for key, value in (lifecycle_event.details or {}).items()
                )
                suffix = f" | {evidence}" if evidence else ""
                lines.append(
                    f"- {symbol} [{lifecycle_event.phase.upper()}] "
                    f"{lifecycle_event.ts.isoformat()} "
                    f"confidence={lifecycle_event.confidence:.2f}{suffix}"
                )
        if ignitions:
            lines.append("\nIgnitions:")
            for ignition_event in ignitions:
                asset = session.get(models.Asset, ignition_event.asset_id)
                symbol = asset.symbol if asset else "?"
                lines.append(
                    f"- {symbol} [{ignition_event.event_type}] "
                    f"{ignition_event.ts.isoformat()} "
                    f"confidence={ignition_event.confidence:.2f}"
                )
        if not terminal and not ignitions:
            lines.append("\nNo terminal transitions or ignitions in the last 24 hours.")
        return "\n".join(lines)

    def _send(self, session: Session, alert: models.Alert) -> None:
        asset = session.get(models.Asset, alert.asset_id)
        symbol = asset.symbol if asset else "?"
        priority, tag = _PUSH_PROFILE.get(alert.alert_type, (2, "bell"))
        title = f"[{symbol}] {alert.alert_type.replace('_', ' ').title()}"
        headers = {
            "Title": title,
            "Priority": str(priority),
            "Tags": tag,
            "Click": f"{self.settings.api_base_url}/tokens/{alert.asset_id}",
        }
        self._post(alert.message, headers)

    def send_rpc_pool_event(self, event: RpcPoolAlert) -> bool:
        """Push one operational RPC event; return False so callers can retry."""
        if not self.enabled:
            return False
        priority, tag = _RPC_PUSH_PROFILE.get(event.kind, (4, "warning"))
        chain = event.chain_slug.upper()
        if event.kind == "zero_healthy_endpoints":
            message = (
                f"{chain} RPC pool has zero healthy endpoints "
                f"({event.total_endpoints} down); failover is exhausted."
            )
            title = f"[{chain}] RPC Pool Exhausted"
        else:
            minutes = (event.down_for_seconds or 0.0) / 60.0
            message = (
                f"{chain} RPC endpoint remains down after {minutes:.0f} minutes: "
                f"{event.url}. Healthy endpoints remaining: {event.healthy_endpoints}/"
                f"{event.total_endpoints}."
            )
            title = f"[{chain}] RPC Endpoint Down"
        self._post(
            message,
            {
                "Title": title,
                "Priority": str(priority),
                "Tags": tag,
                "Click": f"{self.settings.api_base_url}/health",
            },
        )
        return True

    def _post(self, message: str, headers: dict[str, str]) -> None:
        base_url = self.settings.ntfy_base_url.rstrip("/")
        topic = self.settings.ntfy_topic.strip("/")
        with httpx.Client(
            base_url=base_url, timeout=self.settings.ntfy_timeout_seconds
        ) as client:
            response = client.post(
                f"/{topic}", content=message.encode("utf-8"), headers=headers
            )
            response.raise_for_status()


def notify_rpc_pool_event(event: RpcPoolAlert) -> bool:
    """Callback used by pool probe threads and scan-time probe passes."""
    try:
        return NtfyNotifier().send_rpc_pool_event(event)
    except Exception as exc:  # noqa: BLE001 - pool health must not depend on ntfy.
        log.warning("ntfy_rpc_pool_push_failed", chain=event.chain_slug, error=str(exc))
        return False


def notify_lake_budget(
    days_to_full: float,
    pct_full: float,
    growth_rate_bytes_per_hour: float,
    max_bytes: int,
    *,
    settings: Settings | None = None,
) -> bool:
    """Push a retention budget warning when projected lake growth would fill
    the archive capacity within the configured horizon.

    Returns False when ntfy is disabled or the push fails, so callers can
    retry on the next retention pass (the budget health row is still recorded
    either way). A lake at/over capacity escalates to an urgent push.
    """
    notifier = NtfyNotifier()
    if settings is not None:
        notifier.settings = settings
    if not notifier.enabled:
        return False
    urgent = days_to_full <= 0
    if urgent:
        message = (
            f"Lake is at/over the assumed capacity cap ({pct_full:.1f}% full, "
            f"cap {max_bytes:,} B) — prune or move the lake now; compaction "
            "cannot keep up with growth."
        )
        title = "Serpent Circle - Lake Full"
    else:
        message = (
            f"Lake projected to fill in {days_to_full:.1f} days "
            f"({pct_full:.1f}% full, {growth_rate_bytes_per_hour:,.0f} B/h growth, "
            f"cap {max_bytes:,} B) — prune or move the lake before it is full."
        )
        title = "Serpent Circle - Lake Budget Warning"
    try:
        notifier._post(
            message,
            {
                "Title": title,
                "Priority": "5" if urgent else "4",
                "Tags": "rotating_light" if urgent else "thermometer",
                "Click": f"{notifier.settings.api_base_url}/retention/growth",
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - budget health must not depend on ntfy.
        log.warning("ntfy_lake_budget_push_failed", error=str(exc))
        return False


def notify_parity_mismatch(
    mismatch_count: int,
    compared_assets: int,
    decision_ts: datetime,
    examples: list[str],
    *,
    settings: Settings | None = None,
) -> bool:
    """Page a lake-vs-SQL parity mismatch via ntfy.

    Returns False when ntfy is disabled or the push fails, so the next daily
    run retries. ``examples`` are short ``SYMBOL [feature]: sql=... lake=...``
    strings for the first few divergences, so the operator can act without
    opening the DB.
    """
    notifier = NtfyNotifier()
    if settings is not None:
        notifier.settings = settings
    if not notifier.enabled:
        return False
    lines = [
        "Lake-vs-SQL parity check failed: the DuckDB lake read path diverges",
        f"from the live SQL path at decision {decision_ts.isoformat()}.",
        f"{mismatch_count} mismatches across {compared_assets} compared assets.",
    ]
    if examples:
        lines.append("\nFirst mismatches:")
        lines.extend(f"- {example}" for example in examples)
    try:
        notifier._post(
            "\n".join(lines),
            {
                "Title": "Serpent Circle - Lake Parity Mismatch",
                "Priority": "4",
                "Tags": "warning",
                "Click": f"{notifier.settings.api_base_url}/health",
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - parity health must not depend on ntfy.
        log.warning("ntfy_parity_push_failed", error=str(exc))
        return False


def run_notifier(
    session: Session, *, decision_ts: datetime | None = None
) -> dict[str, Any]:
    return NtfyNotifier().flush(session, decision_ts=decision_ts)
