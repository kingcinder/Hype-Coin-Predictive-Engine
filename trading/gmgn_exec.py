"""GMGN execution client (live venue calls).

Order-submitting methods re-check the live gate; read-only status/cancel calls
require only venue configuration (the kill switch must be able to cancel after
the gate closes). Every venue request is signed with the documented GMGN
AK/SK scheme (``X-Ak``/``X-Timestamp``/``X-Signature``, HMAC-SHA256 over
``ak+timestamp+method+path+rawQuery+body``), and no network call happens inside
``queued_write``. Secrets are never logged or returned.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings, secret_value
from common.http import HttpClient
from storage import models
from storage.repository import queued_write
from trading.errors import LiveDisabled, TradeRefused
from trading.live_gate import require_open


@dataclass(frozen=True)
class FillResult:
    order_id: str
    status: str
    price: Decimal
    size_sol: Decimal
    fee_sol: Decimal
    tx_signature: str | None


class GmgnExecutor:
    def __init__(
        self,
        session: Session,
        http: HttpClient | None = None,
        *,
        poll_interval: float = 2.0,
        fill_timeout: float = 60.0,
    ) -> None:
        self.session = session
        self._http = http
        self.poll_interval = poll_interval
        self.fill_timeout = fill_timeout

    # ── guards ──────────────────────────────────────────────────────────
    def _venue_config(self) -> tuple[str, str]:
        """Return ``(api_key, signer_secret)``; fail closed when unusable.

        Configuration is required for read-only order calls too, so a kill
        switch can cancel submitted orders after the live gate has closed.
        """
        s = get_settings()
        if not s.gmgn_api_base or s.gmgn_wallet_address is None:
            raise LiveDisabled("venue_unconfigured")
        api_key = secret_value(s.gmgn_api_key)
        signer_secret = secret_value(s.gmgn_signer_secret)
        if not api_key or not signer_secret:
            # Documented scheme (AK/SK HMAC), but no credentials to apply it:
            # refuse rather than sending an unauthenticated order.
            raise TradeRefused(
                "venue_auth_unimplemented",
                "GMGN_API_KEY and GMGN_SIGNER_SECRET are required to sign venue requests",
            )
        return api_key, signer_secret

    def _guard(self) -> None:
        require_open(self.session)
        self._venue_config()

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Documented GMGN request signature (never includes the secret)."""
        api_key, signer_secret = self._venue_config()
        timestamp = str(int(time.time() * 1000))
        payload = f"{api_key}{timestamp}{method.upper()}{path}{body}"
        signature = hmac.new(
            signer_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {"X-Ak": api_key, "X-Timestamp": timestamp, "X-Signature": signature}

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        # Sign the exact compact JSON bytes httpx will send.
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return self._client().post_json(
            path, json=payload, headers=self._auth_headers("POST", path, body)
        )

    def _get(self, path: str) -> Any:
        return self._client().get_json(path, headers=self._auth_headers("GET", path))

    def _client(self) -> HttpClient:
        if self._http is not None:
            return self._http
        s = get_settings()
        self._http = HttpClient(base_url=s.gmgn_api_base)
        return self._http

    # ── venue ───────────────────────────────────────────────────────────
    def _submit(
        self,
        *,
        side: str,
        mint: str,
        chain: str,
        size_sol: Decimal,
        slippage_pct: Decimal,
        priority_fee_sol: Decimal,
    ) -> str:
        result = self._post(
            "/order",
            {
                "side": side,
                "mint": mint,
                "chain": chain,
                "size_sol": str(size_sol),
                "slippage_pct": str(slippage_pct),
                "priority_fee_sol": str(priority_fee_sol),
            },
        )
        return str((result or {}).get("order_id") or "")

    def order_status(self, order_id: str) -> dict | None:
        self._venue_config()
        return self._get(f"/order/{order_id}")

    def cancel(self, order_id: str) -> bool:
        # Configuration only: the kill switch cancels *after* writing the
        # ``killed`` row, so requiring an open gate here is circular.
        self._venue_config()
        result = self._post("/order/cancel", {"order_id": order_id})
        return bool((result or {}).get("cancelled"))

    def _record(
        self,
        fill: FillResult,
        mint: str,
        chain: str,
        side: str,
        slip: Decimal,
        expected_price: Decimal | None = None,
    ) -> None:
        now = datetime.now(UTC)

        def _persist(write_session: Session) -> None:
            write_session.add(
                models.LiveFill(
                    order_id=fill.order_id,
                    mint=mint,
                    chain=chain,
                    side=side,
                    ts=now,
                    price=fill.price,
                    size_sol=fill.size_sol,
                    fee_sol=fill.fee_sol,
                    slippage_pct=slip,
                    tx_signature=fill.tx_signature,
                    status=fill.status,
                    ledger_ref=None,
                )
            )
            if (
                expected_price is not None
                and expected_price > 0
                and fill.price > 0
                and (
                    (fill.price - expected_price).copy_abs() / expected_price * Decimal("100")
                    > slip
                )
            ):
                write_session.add(
                    models.TradeDivergence(
                        trade_ref=fill.order_id,
                        expected_price=expected_price,
                        fill_price=fill.price,
                        delta_pct=(
                            (fill.price - expected_price) / expected_price * Decimal("100")
                        ).quantize(Decimal("0.0001")),
                        ts=now,
                    )
                )
            if fill.status == "unconfirmed":
                asset_id = write_session.scalar(
                    select(models.Asset.id).where(models.Asset.address == mint)
                )
                if asset_id is not None:
                    write_session.add(
                        models.Alert(
                            asset_id=asset_id,
                            alert_type="live_fill_unconfirmed",
                            threshold_version="live",
                            message=f"live fill unconfirmed for order {fill.order_id}",
                        )
                    )

        queued_write(_persist)

    def _execute(
        self,
        *,
        side: str,
        mint: str,
        chain: str,
        size_sol: Decimal,
        slippage_pct: Decimal,
        priority_fee_sol: Decimal,
        expected_price: Decimal | None = None,
    ) -> FillResult:
        self._guard()
        order_id = self._submit(
            side=side,
            mint=mint,
            chain=chain,
            size_sol=size_sol,
            slippage_pct=slippage_pct,
            priority_fee_sol=priority_fee_sol,
        )
        deadline = time.monotonic() + self.fill_timeout
        last: dict = {}
        while time.monotonic() < deadline:
            status = self.order_status(order_id) or {}
            last = status
            if status.get("status") == "filled":
                break
            time.sleep(self.poll_interval)

        filled = last.get("status") == "filled"
        fill = FillResult(
            order_id=order_id,
            status="filled" if filled else "unconfirmed",
            price=Decimal(str(last.get("price") or 0)),
            size_sol=Decimal(str(last.get("size_sol") or size_sol)),
            fee_sol=Decimal(str(last.get("fee_sol") or 0)),
            tx_signature=last.get("tx_signature"),
        )
        self._record(fill, mint, chain, side, slippage_pct, expected_price)
        return fill

    def buy(
        self,
        *,
        mint: str,
        chain: str,
        size_sol: Decimal,
        slippage_pct: Decimal,
        priority_fee_sol: Decimal,
        expected_price: Decimal | None = None,
    ) -> FillResult:
        return self._execute(
            side="buy",
            mint=mint,
            chain=chain,
            size_sol=size_sol,
            slippage_pct=slippage_pct,
            priority_fee_sol=priority_fee_sol,
            expected_price=expected_price,
        )

    def sell(
        self,
        *,
        mint: str,
        chain: str,
        size_sol: Decimal,
        slippage_pct: Decimal,
        priority_fee_sol: Decimal,
        expected_price: Decimal | None = None,
    ) -> FillResult:
        return self._execute(
            side="sell",
            mint=mint,
            chain=chain,
            size_sol=size_sol,
            slippage_pct=slippage_pct,
            priority_fee_sol=priority_fee_sol,
            expected_price=expected_price,
        )
