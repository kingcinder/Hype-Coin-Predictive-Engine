"""PumpPortal Night Crawler — live pump.fun token-launch watcher.

Watches the pump.fun ecosystem for brand-new token launches in real time —
the earliest possible signal for a hyped memecoin (t0, before liquidity
even exists). Free path: the PumpPortal WebSocket data stream
(``wss://pumpportal.fun/api/data``, ``subscribeNewToken``) requires no API
key. An HTTP recent-pumps endpoint is attempted first; when it is
unavailable or empty the crawler falls back to a short-lived WebSocket tap
that collects new-token events for a few seconds. Every item is a fresh
launch with mint address, deployer wallet, and bonding-curve data.
"""

from __future__ import annotations

import json
from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler

log = get_logger(__name__)

PUMPS_HTTP_URL = "https://api.pumpportal.io/pumps/recent"
PUMPS_WS_URL = "wss://pumpportal.fun/api/data"
# How long the WebSocket fallback listens for new-token events.
WS_TAP_SECONDS = 8.0
# A token is "brand new" (actionable window) if created within this many
# minutes of being observed.
NEW_TOKEN_WINDOW_MINUTES = 30


class PumpPortalCrawler(BaseCrawler):
    """Crawls pump.fun launches via PumpPortal HTTP, falling back to WebSocket."""

    def __init__(self) -> None:
        super().__init__(
            name="pump_portal",
            max_retries=1,  # retries against a live stream are not useful
            retry_delay_seconds=5.0,
            rate_limit_pause=1.5,
            timeout_seconds=12.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items = self._fetch_http()
        if not items:
            log.debug("pump_portal_http_empty", reason="falling back to ws tap")
            items = self._fetch_ws_tap()
        return items

    def _fetch_http(self) -> list[dict[str, Any]]:
        """Attempt the recent-pumps HTTP endpoint (best-effort)."""
        try:
            resp = self.client.get(PUMPS_HTTP_URL)
            if resp.status_code != 200:
                return []
            data = resp.json()
            pumps = data if isinstance(data, list) else (data.get("pumps") or [])
            return [item for pump in pumps if (item := self._pump_item(pump))][:20]
        except Exception as exc:  # noqa: BLE001
            log.debug("pump_portal_http_failed", error=str(exc))
            return []

    def _fetch_ws_tap(self) -> list[dict[str, Any]]:
        """Short-lived WebSocket tap: subscribeNewToken, collect for a few seconds."""
        try:
            from websockets.sync.client import connect
        except ImportError:  # pragma: no cover - websockets is a dev dep
            log.debug("pump_portal_ws_unavailable", reason="websockets not installed")
            return []
        items: list[dict[str, Any]] = []
        try:
            with connect(PUMPS_WS_URL, open_timeout=8.0) as ws:
                ws.send(json.dumps({"method": "subscribeNewToken"}))
                import time

                deadline = time.monotonic() + WS_TAP_SECONDS
                while time.monotonic() < deadline:
                    try:
                        message = json.loads(ws.recv(timeout=2.0))
                    except Exception:  # noqa: BLE001 - timeout keeps polling
                        continue
                    if message.get("txType") == "create":
                        item = self._pump_item(message)
                        if item:
                            items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.debug("pump_portal_ws_failed", error=str(exc))
        return items[:20]

    def _pump_item(self, pump: dict[str, Any]) -> dict[str, Any] | None:
        """Convert a raw pump event/row into a crawler item, or None if unusable."""
        mint = pump.get("mint") or pump.get("address")
        symbol = pump.get("symbol") or ""
        name = pump.get("name") or ""
        if not mint or not symbol:
            return None
        age_minutes = self._launch_age_minutes(pump)
        return {
            "title": name or symbol,
            "text": (
                f"New pump.fun launch: {name} ({symbol}) — mint {mint[:8]}…, "
                f"deployer {str(pump.get('traderPublicKey') or 'unknown')[:8]}…, "
                f"initial buy {pump.get('initialBuy', 0)} SOL"
            ),
            "url": f"https://pump.fun/coin/{mint}",
            "published": utc_now(),
            "source_domain": "pump.fun",
            "source_type": "market_data",
            "metrics": {
                "mint": mint,
                "symbol": symbol,
                "name": name,
                "deployer": pump.get("traderPublicKey"),
                "signature": pump.get("signature"),
                "bonding_curve": pump.get("bondingCurveKey"),
                "initial_buy_sol": pump.get("initialBuy"),
                "market_cap_sol": pump.get("marketCapSol"),
                "v_tokens_in_curve": pump.get("vTokensInBondingCurve"),
                "v_sol_in_curve": pump.get("vSolInBondingCurve"),
                "platform": "pump.fun",
                "new_token": True,
                "launch_age_minutes": age_minutes,
                # Older than the actionable window? Downstream scoring can use
                # this to deprioritize stale launches.
                "old_launch": ((age_minutes or 0) > NEW_TOKEN_WINDOW_MINUTES),
            },
        }

    @staticmethod
    def _launch_age_minutes(pump: dict[str, Any]) -> float | None:
        """Estimate launch age from the creation timestamp when present."""
        ts = pump.get("creationTime") or pump.get("timestamp") or pump.get("createdAt")
        if not ts:
            return None
        try:
            from datetime import UTC, datetime

            if isinstance(ts, (int, float)):
                if ts > 10**12:  # milliseconds
                    ts = ts / 1000
                created = datetime.fromtimestamp(ts, tz=UTC)
            else:
                created = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return round((utc_now() - created).total_seconds() / 60.0, 1)
        except Exception:  # noqa: BLE001
            return None
