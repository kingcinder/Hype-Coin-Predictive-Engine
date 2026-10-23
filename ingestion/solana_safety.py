"""Point-in-time Solana mint and supported LP-account safety evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.http import HttpClient
from common.time import utc_now
from ingestion.rpc_pool import RpcEndpointPool, get_rpc_pool
from storage import models

_BURN_OWNERS = {"1nc1nerator11111111111111111111111111111111"}


@dataclass(frozen=True)
class SolanaSafetyEvidence:
    mint_authority_active: bool | None
    freeze_authority_active: bool | None
    supply: int | None
    decimals: int | None
    transfer_fee_enabled: bool | None
    permanent_delegate_enabled: bool | None
    non_transferable_enabled: bool | None
    default_account_frozen: bool | None
    lp_status: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parsed_info(account: dict[str, Any]) -> dict[str, Any]:
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    return info if isinstance(info, dict) else {}


def decode_mint_account(account: dict[str, Any] | None) -> SolanaSafetyEvidence:
    """Decode jsonParsed SPL/Token-2022 mint fields; absent data stays unknown."""
    info = _parsed_info(account or {})
    if not info:
        return SolanaSafetyEvidence(None, None, None, None, None, None, None, None)

    extensions = info.get("extensions")
    extension_names: set[str] = set()
    default_frozen: bool | None = False if isinstance(extensions, list) else None
    if isinstance(extensions, list):
        for extension in extensions:
            if not isinstance(extension, dict):
                continue
            name = str(extension.get("extension") or "").lower()
            extension_names.add(name)
            if name == "defaultaccountstate":
                state = extension.get("state")
                state_value = state.get("state") if isinstance(state, dict) else state
                if isinstance(state_value, str):
                    default_frozen = state_value.lower() == "frozen"

    def _int_or_none(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    mint_authority = info.get("mintAuthority")
    freeze_authority = info.get("freezeAuthority")
    return SolanaSafetyEvidence(
        mint_authority_active=bool(mint_authority) if "mintAuthority" in info else None,
        freeze_authority_active=bool(freeze_authority) if "freezeAuthority" in info else None,
        supply=_int_or_none(info.get("supply")),
        decimals=_int_or_none(info.get("decimals")),
        transfer_fee_enabled=("transferfeeconfig" in extension_names)
        if isinstance(extensions, list)
        else None,
        permanent_delegate_enabled=("permanentdelegate" in extension_names)
        if isinstance(extensions, list)
        else None,
        non_transferable_enabled=("nontransferable" in extension_names)
        if isinstance(extensions, list)
        else None,
        default_account_frozen=default_frozen,
    )


def decode_lp_status(account: dict[str, Any] | None) -> str:
    """Classify LP ownership only when a parsed provider exposes full evidence."""
    info = _parsed_info(account or {})
    supply = info.get("lpSupply")
    accounts = info.get("lpTokenAccounts")
    if not info.get("lpMint") or not isinstance(accounts, list):
        return "unknown"
    try:
        if supply is None:
            return "unknown"
        total_supply = int(supply)
    except (TypeError, ValueError):
        return "unknown"
    if total_supply < 0:
        return "unknown"
    valid_accounts = [row for row in accounts if isinstance(row, dict)]
    try:
        amounts = [
            (
                str(row.get("owner") or ""),
                int(row["amount"]) if row.get("amount") is not None else -1,
            )
            for row in valid_accounts
        ]
        if any(amount < 0 for _, amount in amounts):
            return "unknown"
    except (TypeError, ValueError):
        return "unknown"
    if sum(amount for _, amount in amounts) < total_supply:
        return "unknown"
    if (
        total_supply == 0
        or sum(amount for owner, amount in amounts if owner in _BURN_OWNERS) >= total_supply
    ):
        return "burned"
    if (
        sum(
            amount
            for row, (_, amount) in zip(valid_accounts, amounts, strict=True)
            if row.get("locked") is True
        )
        >= total_supply
    ):
        return "locked"
    return "unlocked"


def fetch_account_info(
    address: str,
    *,
    pool: RpcEndpointPool | None = None,
    http: Any | None = None,
) -> dict[str, Any] | None:
    """Fetch one jsonParsed account via the health-aware RPC pool."""
    endpoint_pool = pool or get_rpc_pool("solana")
    url: str | None = None
    client: Any | None = None
    try:
        url = endpoint_pool.pick()
        client = http or HttpClient(base_url=url)
        response = client.post_json(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [address, {"encoding": "jsonParsed"}],
            },
        )
        value = (response or {}).get("result", {}).get("value")
        if not isinstance(value, dict):
            endpoint_pool.mark_failure(url)
            return None
        endpoint_pool.mark_success(url)
        return value
    except Exception:  # noqa: BLE001 - per-asset RPC failures are missing evidence.
        if url is not None:
            endpoint_pool.mark_failure(url)
        return None
    finally:
        if http is None and client is not None:
            client.close()


def fetch_mint_account(
    mint: str,
    *,
    pool: RpcEndpointPool | None = None,
    http: Any | None = None,
) -> dict[str, Any] | None:
    return fetch_account_info(mint, pool=pool, http=http)


def collect_asset_safety(
    session: Session,
    asset: models.Asset,
    *,
    observed_at: datetime | None = None,
    pool: RpcEndpointPool | None = None,
) -> models.SolanaSafetySnapshot:
    """Persist latest mint evidence and any supported parsed LP classification."""
    observed = observed_at or utc_now()
    account = fetch_mint_account(asset.address, pool=pool)
    evidence = decode_mint_account(account) if account is not None else None
    if evidence is not None:
        pair_addresses = session.scalars(
            select(models.Pool.address)
            .join(models.Pair, models.Pair.pool_id == models.Pool.id)
            .where(models.Pair.base_asset_id == asset.id)
        ).all()
        for address in pair_addresses:
            pool_account = fetch_account_info(str(address), pool=pool)
            lp_status = decode_lp_status(pool_account)
            if lp_status != "unknown":
                evidence = SolanaSafetyEvidence(**{**evidence.as_dict(), "lp_status": lp_status})
                break
    row = models.SolanaSafetySnapshot(
        asset_id=asset.id,
        observed_at=observed,
        status="ok" if evidence is not None else "missing",
        evidence=evidence.as_dict() if evidence is not None else {},
    )
    session.add(row)
    session.flush()
    return row
