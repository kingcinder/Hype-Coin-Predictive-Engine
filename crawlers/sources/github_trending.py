"""GitHub Trending Night Crawler — dev-activity signals from the GitHub API.

Tracks newly-created crypto/token repositories (the supply side of the
hype-coin pipeline): many new repo launches in a short window is an early
indicator of a narrative heating up, before price data even exists.
Uses the GitHub search API — free, no token required for 10 req/min
(5000 req/h with a token). Optional token via config.
"""

from __future__ import annotations

from typing import Any

from common.logging import get_logger
from common.time import utc_now
from crawlers.base import BaseCrawler

log = get_logger(__name__)

# Keywords that mark a repo as launch/token-related when found in its
# description, name, or topics — used to score signal relevance.
_LAUNCH_KEYWORDS = (
    "token",
    "memecoin",
    "meme coin",
    "erc20",
    "bep20",
    "spl",
    "presale",
    "airdrop",
    "launch",
    "deploy",
    "smart contract",
    "web3",
    "defi",
    "dex",
    "liquidity pool",
    "staking",
    "nft mint",
)


class GitHubTrendingCrawler(BaseCrawler):
    """Crawls GitHub search for newly created crypto/token repositories."""

    SEARCH_URL = "https://api.github.com/search/repositories"

    def __init__(self, search_query: str | None = None, token: str | None = None) -> None:
        self._search_query = search_query or "crypto token memecoin created:>30d"
        self._token = token
        super().__init__(
            name="github_trending",
            max_retries=2,
            retry_delay_seconds=5.0,
            rate_limit_pause=2.0,
            timeout_seconds=20.0,
        )

    def _create_client_headers(self) -> dict[str, str]:
        """Inject the GitHub Authorization header when a token is provided."""
        headers = super()._create_client_headers()
        headers["Accept"] = "application/vnd.github+json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def fetch_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_search_repos())
        return items

    def _fetch_search_repos(self) -> list[dict[str, Any]]:
        """Search GitHub for recently created crypto/token repositories."""
        try:
            resp = self.client.get(
                self.SEARCH_URL,
                params={
                    "q": self._search_query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": "30",
                },
            )
            if resp.status_code != 200:
                log.debug(
                    "github_search_non_200",
                    status=resp.status_code,
                    detail=resp.text[:200],
                )
                return []
            data = resp.json()
            repos = data.get("items") or []

            items: list[dict[str, Any]] = []
            for repo in repos[:25]:
                topics = repo.get("topics") or []
                description = repo.get("description") or ""
                name = repo.get("full_name", "")
                haystack = f"{name} {description} {' '.join(topics)}".lower()
                relevance = sum(1 for kw in _LAUNCH_KEYWORDS if kw in haystack)

                items.append(
                    {
                        "title": name,
                        "text": description[:300] or f"New crypto repo: {name}",
                        "url": repo.get("html_url", ""),
                        "published": utc_now(),
                        "source_domain": "github.com",
                        "source_type": "developer_activity",
                        "metrics": {
                            "repo_full_name": name,
                            "stars": repo.get("stargazers_count", 0),
                            "forks": repo.get("forks_count", 0),
                            "language": repo.get("language"),
                            "topics": topics[:8],
                            "created_at": repo.get("created_at"),
                            "pushed_at": repo.get("pushed_at"),
                            "launch_relevance": relevance,
                            "new_repo": True,
                        },
                    }
                )
            return items
        except Exception as exc:  # noqa: BLE001
            log.debug("github_search_failed", error=str(exc))
            return []
