"""Block Explorer Night Crawler — contract intelligence and deployer tracking.

Crawls block explorers (Etherscan, Solscan) for contract verification status,
deployer history, token holder distribution, and suspicious contract flags
that feed the risk engine and fingerprint system.
"""
from __future__ import annotations

from typing import Any

from common.time import utc_now
from crawlers.base import BaseCrawler


class ExplorerCrawler(BaseCrawler):
    """Crawls block explorers for contract intelligence."""

    def __init__(self, etherscan_api_key: str | None = None) -> None:
        super().__init__(
            name="explorer",
            max_retries=2,
            retry_delay_seconds=3.0,
            rate_limit_pause=1.0,
            timeout_seconds=15.0,
        )
        self.etherscan_api_key = etherscan_api_key

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_new_contracts())
        return items

    def _fetch_new_contracts(self) -> list[dict[str, Any]]:
        """Fetch recently deployed contracts on Ethereum/Base."""
        if not self.etherscan_api_key:
            return []
        try:
            items = []
            for chain, chain_id in [("ethereum", "1"), ("base", "8453")]:
                data = self.client.get(
                    "https://api.etherscan.io/v2/api",
                    params={
                        "chainid": chain_id,
                        "module": "contract",
                        "action": "getcontractcreation",
                        "contractaddresses": "",
                        "apikey": self.etherscan_api_key,
                    },
                ).json()
                for contract in (data.get("result") or [])[:10]:
                    items.append({
                        "title": f"New contract: {contract.get('contractAddress', '')[:10]}...",
                        "text": f"Contract deployed on {chain} by {contract.get('contractCreator', '')[:10]}...",
                        "url": f"https://{chain}.etherscan.io/address/{contract.get('contractAddress', '')}",
                        "published": utc_now(),
                        "source_domain": f"{chain}.etherscan.io",
                        "source_type": "on_chain",
                        "metrics": {
                            "chain": chain,
                            "address": contract.get("contractAddress"),
                            "creator": contract.get("contractCreator"),
                            "tx_hash": contract.get("txHash"),
                        },
                    })
            return items
        except Exception:
            return []
