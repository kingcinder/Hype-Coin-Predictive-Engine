"""Block Explorer Night Crawler — contract intelligence and deployer tracking.

Crawls block explorers (Etherscan, Solscan) for contract verification status,
deployer history, token holder distribution, and suspicious contract flags
that feed the risk engine and fingerprint system.
"""

from __future__ import annotations

from typing import Any

from common.time import utc_now
from crawlers.base import BaseCrawler
from ingestion.source_clients import get_rpc_url, rpc_pool_for


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
        # Legacy: contract discovery no longer uses Etherscan (RPC scan instead).
        # Kept so existing call sites/tests keep working.
        self.etherscan_api_key = etherscan_api_key

    def skip_reason(self) -> str | None:
        """Contract discovery now runs over the RPC pool, no explorer key needed."""
        return None

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_new_contracts())
        return items

    def _fetch_new_contracts(self) -> list[dict[str, Any]]:
        """Fetch recently deployed contracts on Ethereum/Base.

        The Etherscan V2 ``getcontractcreation`` endpoint requires a list of
        contract addresses to look up, so it cannot enumerate new deployments;
        calling it with an empty list always errors ("Missing contract
        addresses"). Instead, scan the latest blocks over the RPC pool for
        transactions with no ``to`` (contract creations) and resolve the
        deployed address from the receipt.
        """
        items: list[dict[str, Any]] = []
        for chain in ("ethereum", "base"):
            try:
                items.extend(self._scan_chain_for_creations(chain))
            except Exception:
                continue
        return items[:20]

    def _rpc_endpoints(self, chain_slug: str) -> list[str]:
        """Primary RPC URL first, then the pool's healthy endpoints."""
        urls: list[str] = []
        primary = get_rpc_url(chain_slug)
        if primary:
            urls.append(primary)
        pool = rpc_pool_for(chain_slug)
        if pool is not None:
            for state in sorted(pool.snapshot(), key=lambda s: (s.down, -s.health)):
                if state.url not in urls:
                    urls.append(state.url)
        return urls

    def _rpc_post(self, chain_slug: str, method: str, params: list[Any]) -> Any:
        """One JSON-RPC call, failing over across the chain's pool endpoints."""
        last_error: Exception | None = None
        for url in self._rpc_endpoints(chain_slug):
            try:
                data = self.client.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                ).json()
            except Exception as exc:  # noqa: BLE001 - try the next endpoint.
                last_error = exc
                continue
            result = data.get("result") if isinstance(data, dict) else None
            if result is not None:
                return result
            last_error = RuntimeError(f"{chain_slug}: {method} returned no result")
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"{chain_slug}: no RPC endpoint for {method}")

    def _scan_chain_for_creations(self, chain_slug: str) -> list[dict[str, Any]]:
        """New contracts in the latest 12 blocks of one chain."""
        latest = self._rpc_post(chain_slug, "eth_blockNumber", [])
        latest_block = int(str(latest), 16)
        items: list[dict[str, Any]] = []
        for block_number in range(latest_block, max(latest_block - 12, -1), -1):
            block = self._rpc_post(chain_slug, "eth_getBlockByNumber", [hex(block_number), True])
            if not isinstance(block, dict):
                continue
            for tx in block.get("transactions") or []:
                if not isinstance(tx, dict) or tx.get("to"):
                    continue
                tx_hash = str(tx.get("hash") or "")
                creator = str(tx.get("from") or "")
                address = self._creation_address(chain_slug, tx_hash)
                items.append(
                    {
                        "title": f"New contract: {(address or tx_hash)[:10]}...",
                        "text": f"Contract deployed on {chain_slug} by {creator[:10]}...",
                        "url": f"https://{chain_slug}.etherscan.io/tx/{tx_hash}"
                        if chain_slug == "ethereum"
                        else f"https://basescan.org/tx/{tx_hash}",
                        "published": utc_now(),
                        "source_domain": f"{chain_slug}.etherscan.io"
                        if chain_slug == "ethereum"
                        else "basescan.org",
                        "source_type": "on_chain",
                        "metrics": {
                            "chain": chain_slug,
                            "address": address,
                            "creator": creator,
                            "tx_hash": tx_hash,
                        },
                    }
                )
                if len(items) >= 10:
                    return items
        return items

    def _creation_address(self, chain_slug: str, tx_hash: str) -> str | None:
        """Deployed contract address from the creation tx's receipt."""
        if not tx_hash:
            return None
        try:
            receipt = self._rpc_post(chain_slug, "eth_getTransactionReceipt", [tx_hash])
        except Exception:
            return None
        if isinstance(receipt, dict):
            address = receipt.get("contractAddress")
            return str(address) if address else None
        return None
