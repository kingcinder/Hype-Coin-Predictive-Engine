"""Provider keys embedded in RPC URLs must not be persisted or surfaced."""

from __future__ import annotations

from sqlalchemy import select

from common.config import get_settings
from common.urls import redact_url
from ingestion import rpc_pool
from storage import models

SECRET = "00000000-0000-4000-8000-000000000000"
HELIUS = f"https://mainnet.helius-rpc.com/?api-key={SECRET}"


def test_redact_url_masks_query_credentials() -> None:
    assert redact_url(HELIUS) == "https://mainnet.helius-rpc.com/?api-key=***"
    assert (
        redact_url("https://rpc.example/v1?key=abc&network=mainnet")
        == "https://rpc.example/v1?key=***&network=mainnet"
    )
    assert redact_url("https://api.mainnet-beta.solana.com") == (
        "https://api.mainnet-beta.solana.com"
    )


def test_redact_url_masks_alchemy_path_token() -> None:
    alchemy = "https://eth-mainnet.g.alchemy.com/v2/fake-key-for-tests"
    assert redact_url(alchemy) == "https://eth-mainnet.g.alchemy.com/v2/***"


def _configure_helius(monkeypatch, settings) -> None:
    monkeypatch.setattr(settings, "helius_api_key", SECRET)
    monkeypatch.setattr(settings, "rpc_pool_enabled", True)
    monkeypatch.setattr(settings, "solana_rpc_pool_csv", "https://solana-rpc.publicnode.com")
    rpc_pool.get_rpc_pool.cache_clear()


def test_pool_snapshot_persists_redacted_url(session, monkeypatch) -> None:
    settings = get_settings()
    _configure_helius(monkeypatch, settings)
    try:
        rpc_pool.persist_pool_snapshots(session)
        session.commit()
        urls = session.scalars(select(models.RpcPoolSnapshot.url)).all()
    finally:
        rpc_pool.get_rpc_pool.cache_clear()

    assert urls
    assert SECRET not in "".join(urls)
    assert any("api-key=***" in url for url in urls)


def test_pool_health_message_redacts_down_endpoint(session, monkeypatch) -> None:
    settings = get_settings()
    _configure_helius(monkeypatch, settings)
    try:
        pool = rpc_pool.get_rpc_pool("solana")
        for _ in range(settings.rpc_pool_failure_threshold + 1):
            pool.mark_failure(HELIUS)
        rpc_pool._pool_health_row(session, "solana")
        session.commit()
        row = session.scalar(
            select(models.SystemHealth)
            .where(models.SystemHealth.component == "rpc_pool:solana")
            .order_by(models.SystemHealth.id.desc())
        )
    finally:
        rpc_pool.get_rpc_pool.cache_clear()

    assert row is not None
    assert SECRET not in (row.message or "")
    assert "api-key=***" in (row.message or "")
