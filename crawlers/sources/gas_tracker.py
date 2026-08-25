"""Ethereum/Solana Gas Tracker Night Crawler — fee-market pressure as a hype proxy.

Pulls live gas prices from the free Etherscan gas oracle (no API key required
for the basic gastracker module) and Solana priority fees from the configured
RPC. Sustained gas spikes on Ethereum are an early, cheap proxy for launch /
mint / trading activity; the raw snapshot feeds the signal scoring pipeline
alongside every other crawler.
"""

from __future__ import annotations

from typing import Any

from common.config import get_settings
from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler

log = get_logger(__name__)


class GasTrackerCrawler(BaseCrawler):
    """Crawls Ethereum + Solana network gas prices as market-pressure signals."""

    ETH_GASORACLE_URL = "https://api.etherscan.io/api"
    # gwei levels used to label the fee regime in each snapshot
    SPIKE_GWEI = 100.0
    HIGH_GWEI = 40.0

    def __init__(
        self,
        *,
        etherscan_api_key: str | None = None,
        include_solana: bool = True,
    ) -> None:
        self._etherscan_api_key = etherscan_api_key
        self._include_solana = include_solana
        super().__init__(
            name="gas_tracker",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=1.5,
            timeout_seconds=15.0,
        )

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        eth = self._fetch_eth_gas()
        if eth:
            items.append(eth)
        if self._include_solana:
            sol = self._fetch_solana_priority_fee()
            if sol:
                items.append(sol)
        return items

    def _fetch_eth_gas(self) -> dict[str, Any] | None:
        """Fetch Ethereum gas prices from the free Etherscan gas oracle."""
        try:
            params: dict[str, Any] = {
                "module": "gastracker",
                "action": "gasoracle",
            }
            if self._etherscan_api_key:
                params["apikey"] = self._etherscan_api_key
            data = self.client.get(self.ETH_GASORACLE_URL, params=params).json()
            result = data.get("result") or {}
            if not isinstance(result, dict):
                return None
            safe = _to_float(result.get("SafeGasPrice"))
            propose = _to_float(result.get("ProposeGasPrice"))
            fast = _to_float(result.get("FastGasPrice"))
            base_fee = _to_float(result.get("suggestBaseFee"))
            if not fast:
                return None

            if fast >= self.SPIKE_GWEI:
                regime = "spike"
            elif fast >= self.HIGH_GWEI:
                regime = "high"
            else:
                regime = "normal"

            return {
                "title": f"Ethereum gas {regime}: {fast:.1f} gwei fast",
                "text": (
                    f"Ethereum network gas: safe {safe:.1f}, propose {propose:.1f}, "
                    f"fast {fast:.1f} gwei, base fee {base_fee:.1f} gwei — {regime} regime. "
                    "Sustained spikes often precede launch/mint activity."
                ),
                "url": "https://etherscan.io/gastracker",
                "published": utc_now(),
                "source_domain": "etherscan.io",
                "source_type": "market_data",
                "metrics": {
                    "chain": "ethereum",
                    "safe_gwei": round(safe, 1),
                    "propose_gwei": round(propose, 1),
                    "fast_gwei": round(fast, 1),
                    "base_fee_gwei": round(base_fee, 1),
                    "regime": regime,
                    "gas_spike": regime == "spike",
                },
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("gas_tracker_eth_failed", error=str(exc))
            return None

    def _fetch_solana_priority_fee(self) -> dict[str, Any] | None:
        """Fetch recent Solana priority fees via the configured RPC."""
        try:
            settings = get_settings()
            rpc_url = settings.rpc_url_for_chain("solana") or settings.solana_rpc_url
            resp = self.client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getRecentPrioritizationFees",
                    "params": [],
                },
            )
            data = resp.json()
            result = data.get("result") or []
            fees = [
                int(item.get("prioritizationFee", 0))
                for item in result
                if isinstance(item, dict) and item.get("prioritizationFee") is not None
            ]
            if not fees:
                return None
            median = sorted(fees)[len(fees) // 2]
            return {
                "title": f"Solana priority fee: {median} lamports median",
                "text": (
                    f"Solana recent priority fees: median {median} lamports "
                    f"across {len(fees)} recent slots. Rising priority fees signal "
                    "competing bot/deploy activity."
                ),
                "url": "https://solscan.io/",
                "published": utc_now(),
                "source_domain": "solscan.io",
                "source_type": "market_data",
                "metrics": {
                    "chain": "solana",
                    "median_priority_fee_lamports": median,
                    "sample_count": len(fees),
                },
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("gas_tracker_solana_failed", error=str(exc))
            return None


def _to_float(value: Any) -> float:
    """Safely coerce a gas-oracle value to float."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
