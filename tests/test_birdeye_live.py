"""Opt-in live smoke checks for Birdeye endpoint paths.

The normal suite is hermetic: these tests are skipped unless both
``BIRDEYE_LIVE_SMOKE=1`` and a Birdeye API key are configured. The flag is
deliberate because a developer's ``.env`` may contain a valid key; merely
having a key must never turn an ordinary test run into a live network call.
"""

from __future__ import annotations

import os

import pytest

from common.config import get_settings, secret_value
from ingestion.birdeye_client import BirdeyeClient

LIVE_SMOKE = os.getenv("BIRDEYE_LIVE_SMOKE") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE_SMOKE,
    reason="set BIRDEYE_LIVE_SMOKE=1 to run live Birdeye endpoint smoke checks",
)

# A long-lived Solana mint with a stable overview record.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _configured_key() -> str:
    key = secret_value(get_settings().birdeye_api_key)
    if not key:
        pytest.skip("BIRDEYE_API_KEY is not configured")
    return key


def test_live_birdeye_overview_trending_and_gainers() -> None:
    client = BirdeyeClient(api_key=_configured_key())
    try:
        overview = client.token_overview(USDC_MINT)
        trending = client.trending_tokens(limit=3)
        gainers = client.top_gainers(limit=3)
    finally:
        client.close()

    # A moved path raises from the HTTP client; a changed response shape makes
    # the parsing methods return None/[] and fails these assertions.
    assert isinstance(overview, dict), "token_overview returned no snapshot"
    assert overview.get("address") == USDC_MINT
    assert overview.get("price") is not None

    assert isinstance(trending, list) and trending, "trending_tokens returned no tokens"
    assert all(isinstance(item, dict) and item.get("address") for item in trending)

    assert isinstance(gainers, list) and gainers, "top_gainers returned no tokens"
    assert all(isinstance(item, dict) and item.get("address") for item in gainers)
