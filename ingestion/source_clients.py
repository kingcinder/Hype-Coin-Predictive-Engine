from __future__ import annotations

from functools import partial
from typing import Any

import httpx

from common.config import get_settings
from common.http import HttpClient
from ingestion.rpc_pool import POOL_CHAINS, RpcEndpointPool, get_rpc_pool
from ops.notifier import notify_rpc_pool_event

PROBE_METHODS = {
    "solana": "getHealth",
    "base": "eth_blockNumber",
    "ethereum": "eth_blockNumber",
}


def probe_rpc_endpoint(chain_slug: str, url: str) -> bool:
    """Fast-fail liveness probe for an RPC endpoint of a given chain.

    Solana: ``getHealth``; EVM chains: ``eth_blockNumber``. A lightweight POST
    with a short timeout, bypassing the retrying ``HttpClient`` so a dead
    endpoint is detected in seconds. Used by the background probe threads and
    the per-scan probe pass. Never raises.
    """
    method = PROBE_METHODS.get(chain_slug)
    if method is None:
        return False
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": []},
                headers={"User-Agent": "serpent-hype-coin-engine/0.1"},
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        return bool((payload or {}).get("result"))
    except Exception:  # noqa: BLE001 - a probe must never raise.
        return False


def rpc_pool_for(chain_slug: str) -> RpcEndpointPool | None:
    """The health-scored endpoint pool for a chain, or None when disabled."""
    settings = get_settings()
    if not settings.rpc_pool_enabled:
        return None
    pool = get_rpc_pool(chain_slug)
    return pool if pool.enabled else None


def ensure_background_probe() -> bool:
    """Idempotently start background probe threads for every configured chain.

    One daemon thread per chain pool probes that chain's downed endpoints every
    ``RPC_POOL_PROBE_INTERVAL_SECONDS`` so they recover without waiting for the
    20-pick probe slot. Safe to call from the worker, the scheduler, and
    repeatedly.
    """
    settings = get_settings()
    if not settings.rpc_pool_enabled or not settings.rpc_pool_background_probe_enabled:
        return False
    started = False
    for chain_slug in POOL_CHAINS:
        pool = rpc_pool_for(chain_slug)
        if pool is None:
            continue
        started |= pool.start_background_probe(
            partial(probe_rpc_endpoint, chain_slug),
            interval_seconds=settings.rpc_pool_probe_interval_seconds,
            alert_callback=notify_rpc_pool_event,
        )
    return started


def get_rpc_url(chain_slug: str) -> str | None:
    """Best available RPC URL for a chain, honoring the §2 endpoint pool.

    Every chain with a curated pool rotates through it (health-scored
    failover); chains without one fall back to their single configured URL.
    """
    settings = get_settings()
    pool = rpc_pool_for(chain_slug)
    if pool is not None:
        return pool.pick()
    return settings.rpc_url_for_chain(chain_slug)


class DexScreenerClient:
    def __init__(self) -> None:
        self.http = HttpClient(base_url="https://api.dexscreener.com")

    def close(self) -> None:
        self.http.close()

    def latest_token_profiles(self) -> list[dict[str, Any]]:
        data = self.http.get_json("/token-profiles/latest/v1")
        return data if isinstance(data, list) else []

    def top_boosts(self) -> list[dict[str, Any]]:
        data = self.http.get_json("/token-boosts/top/v1")
        return data if isinstance(data, list) else []

    def token_pairs(self, chain_slug: str, token_address: str) -> list[dict[str, Any]]:
        data = self.http.get_json(f"/token-pairs/v1/{chain_slug}/{token_address}")
        return data if isinstance(data, list) else []

    def search_pairs(self, query: str) -> list[dict[str, Any]]:
        data = self.http.get_json("/latest/dex/search", params={"q": query})
        if isinstance(data, dict):
            return data.get("pairs") or []
        return []


class GeckoTerminalClient:
    NETWORKS = {"solana": "solana", "base": "base", "ethereum": "eth"}

    def __init__(self) -> None:
        self.http = HttpClient(base_url="https://api.geckoterminal.com")

    def close(self) -> None:
        self.http.close()

    def new_pools(self, chain_slug: str) -> list[dict[str, Any]]:
        network = self.NETWORKS.get(chain_slug, chain_slug)
        data = self.http.get_json(f"/api/v2/networks/{network}/new_pools")
        if isinstance(data, dict):
            items = data.get("data") or []
            return items if isinstance(items, list) else []
        return []


class SolanaRpcClient:
    def __init__(
        self,
        rpc_url: str | None = None,
        *,
        pool: RpcEndpointPool | None = None,
        pool_enabled: bool | None = None,
    ) -> None:
        settings = get_settings()
        self._pool: RpcEndpointPool | None = None
        self._pool_url: str | None = None
        use_pool = settings.rpc_pool_enabled if pool_enabled is None else pool_enabled
        if rpc_url is None and use_pool:
            if pool is None:
                pool = get_rpc_pool("solana")
            if pool.enabled:
                self._pool = pool
                rpc_url = pool.pick()
                self._pool_url = rpc_url
        self.http = HttpClient(
            base_url=rpc_url or settings.rpc_url_for_chain("solana") or settings.solana_rpc_url
        )

    def close(self) -> None:
        self.http.close()

    def rpc(self, method: str, params: list[Any] | None = None) -> Any:
        try:
            data = self.http.post_json(
                "/",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
            )
        except Exception:
            if self._pool is not None:
                self._pool.mark_failure(self._pool_url or self.http.base_url)
            raise
        if self._pool is not None:
            self._pool.mark_success(self._pool_url or self.http.base_url)
        return data

    def get_health(self) -> str:
        data = self.rpc("getHealth")
        return str(data.get("result") if isinstance(data, dict) else data)

    def get_token_supply(self, mint: str) -> float | None:
        data = self.rpc("getTokenSupply", [mint])
        try:
            return float(data["result"]["value"]["uiAmount"])
        except (KeyError, TypeError, ValueError):
            return None

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        data = self.rpc("getTokenLargestAccounts", [mint])
        try:
            value = data["result"]["value"]
            return value if isinstance(value, list) else []
        except (KeyError, TypeError):
            return []

    def get_recent_signatures(self, mint: str, *, limit: int = 100) -> list[dict[str, Any]]:
        data = self.rpc("getSignaturesForAddress", [mint, {"limit": limit}])
        value = data.get("result") if isinstance(data, dict) else None
        return value if isinstance(value, list) else []


class EVMHolderClient:
    """Top-holder snapshots for EVM tokens via the free public Blockscout v2
    API (no key): token info (``total_supply``) + the token-holders list.

    Public Blockscout instances rate-limit unauthenticated requests (~3/min),
    so callers must pause between tokens (``EVM_HOLDER_RPC_PAUSE_SECONDS``).
    Balances and supply are returned in the token's raw units — pct-of-supply
    and ranking are invariant to that common scale, and the SQL and lake read
    paths both consume the same numbers, so the holder features agree.
    """

    BLOCKSCOUT_BASES = {
        "base": "https://base.blockscout.com",
        "ethereum": "https://eth.blockscout.com",
    }

    def __init__(self, chain_slug: str) -> None:
        base = self.BLOCKSCOUT_BASES.get(chain_slug)
        if not base:
            raise ValueError(f"no blockscout instance for chain {chain_slug!r}")
        self.chain_slug = chain_slug
        self.http = HttpClient(base_url=base)

    def close(self) -> None:
        self.http.close()

    def token_supply(self, address: str) -> float | None:
        """Total supply in raw token units (``total_supply``), or None when
        the token info call fails."""
        data = self.http.get_json(f"/api/v2/tokens/{address}")
        if not isinstance(data, dict):
            return None
        try:
            return float(data["total_supply"])
        except (KeyError, TypeError, ValueError):
            return None

    def top_holders(self, address: str) -> list[dict[str, Any]]:
        """The token-holders page normalized to the canonical evidence shape
        the lake reconstruction parses (``[{address, uiAmountString}]``)."""
        data = self.http.get_json(f"/api/v2/tokens/{address}/holders")
        if not isinstance(data, dict):
            return []
        items = data.get("items") or []
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            holder = item.get("address_hash")
            address_hash = holder.get("hash") if isinstance(holder, dict) else None
            raw_value = item.get("value")
            if not address_hash or raw_value is None:
                continue
            try:
                balance = float(raw_value)
            except (TypeError, ValueError):
                continue
            out.append({"address": str(address_hash), "uiAmountString": str(balance)})
        return out


class EtherscanClient:
    CHAIN_IDS = {"ethereum": "1", "base": "8453"}

    def __init__(self) -> None:
        self.settings = get_settings()
        self.http = HttpClient(base_url="https://api.etherscan.io")

    def close(self) -> None:
        self.http.close()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.etherscan_api_key)

    def token_supply(self, chain_slug: str, contract_address: str) -> float | None:
        if not self.enabled:
            return None
        params = {
            "chainid": self.CHAIN_IDS.get(chain_slug, "1"),
            "module": "stats",
            "action": "tokensupply",
            "contractaddress": contract_address,
            "apikey": self.settings.etherscan_api_key,
        }
        data = self.http.get_json("/v2/api", params=params)
        try:
            return float(data["result"])
        except (KeyError, TypeError, ValueError):
            return None


class StaticWebsiteClient:
    def __init__(self) -> None:
        self.http = HttpClient()

    def close(self) -> None:
        self.http.close()

    def probe(self, url: str) -> dict[str, Any]:
        response_text = ""
        try:
            response = self.http._client.get(url)
            response.raise_for_status()
            response_text = response.text[:4096]
            return {
                "url": str(response.url),
                "status_code": response.status_code,
                "ok": True,
                "title_present": "<title" in response_text.lower(),
                "content_sample": response_text[:512],
            }
        except Exception as exc:  # noqa: BLE001 - evidence capture, not broad success.
            return {
                "url": url,
                "ok": False,
                "error": str(exc),
                "content_sample": response_text[:512],
            }


class PublicGitHubClient:
    def __init__(self) -> None:
        self.http = HttpClient(base_url="https://api.github.com")

    def close(self) -> None:
        self.http.close()

    def repo_metadata(self, github_url: str) -> dict[str, Any] | None:
        marker = "github.com/"
        if marker not in github_url.lower():
            return None
        owner_repo = github_url.split(marker, 1)[1].strip("/").split("/")
        if len(owner_repo) < 2:
            return None
        owner, repo = owner_repo[0], owner_repo[1]
        data = self.http.get_json(f"/repos/{owner}/{repo}")
        if not isinstance(data, dict):
            return None
        return {
            "full_name": data.get("full_name"),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "pushed_at": data.get("pushed_at"),
            "html_url": data.get("html_url"),
        }
