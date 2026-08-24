from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from common.config import get_settings
from common.logging import get_logger

log = get_logger(__name__)


class HttpClient:
    def __init__(self, *, base_url: str = "", timeout: float | None = None, headers: dict[str, str] | None = None) -> None:
        settings = get_settings()
        self.base_url = base_url
        merged_headers = {"User-Agent": "serpent-hype-coin-engine/0.1"}
        if headers:
            merged_headers.update(headers)
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout or settings.request_timeout_seconds,
            headers=merged_headers,
            follow_redirects=True,
        )
        self._max_attempts = settings.max_request_retries

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_json(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self._request_json("GET", path, params=params)

    def post_json(self, path: str, *, json: Mapping[str, Any] | None = None) -> Any:
        return self._request_json("POST", path, json=json)

    @retry(
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError)
        ),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()
