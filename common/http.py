from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.request import getproxies

import httpx

# URLPattern is where httpx.Client validates proxy-mount keys at construction
# time (the exact site of the H26 InvalidURL crash); it is not re-exported at
# the package top level.
from httpx._utils import URLPattern
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from common.config import get_settings
from common.logging import get_logger

log = get_logger(__name__)


def _is_ipv4(hostname: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(hostname.strip().strip("[]")), ipaddress.IPv4Address)
    except ValueError:
        return False


def _is_ipv6(hostname: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(hostname.strip().strip("[]")), ipaddress.IPv6Address)
    except ValueError:
        return False


def sanitized_proxy_map() -> dict[str, str | None]:
    """Build an explicit proxy map from the environment (H26).

    Mirrors httpx's own environment-proxy semantics — ``HTTP_PROXY`` /
    ``HTTPS_PROXY`` / ``ALL_PROXY`` plus ``NO_PROXY`` bypass entries — but
    validates every generated mount key the same way ``httpx.Client`` does at
    construction time, dropping entries httpx cannot parse instead of letting
    the whole HTTP layer fail. (Known trigger: bare IPv6 literals in
    ``NO_PROXY``, which httpx turns into the unparseable pattern
    ``all://*[fd8b:...]`` and then raises ``InvalidURL`` from the
    ``Client`` constructor.)

    Returns a map of proxy-mount pattern -> proxy URL consumed by
    :func:`build_httpx_client` as ``mounts``; ``None`` values mean "bypass the
    proxy for this pattern".
    """
    proxy_info = getproxies()
    mounts: dict[str, str | None] = {}
    for scheme in ("http", "https", "all"):
        if proxy_info.get(scheme):
            hostname = proxy_info[scheme]
            url = hostname if "://" in hostname else f"http://{hostname}"
            try:
                httpx.URL(url)
            except Exception:
                log.warning("dropping unparseable proxy URL", entry=url)
                continue
            mounts[f"{scheme}://"] = url

    no_proxy_hosts = [host.strip() for host in proxy_info.get("no", "").split(",")]
    for hostname in no_proxy_hosts:
        if not hostname:
            continue
        if hostname == "*":
            # NO_PROXY=* bypasses everything; no proxy mounts at all.
            return {}
        if "://" in hostname:
            key = hostname
        elif _is_ipv4(hostname):
            key = f"all://{hostname.strip('[]')}"
        elif _is_ipv6(hostname):
            key = f"all://[{hostname.strip('[]')}]"
        elif hostname.lower() == "localhost":
            key = f"all://{hostname}"
        else:
            key = f"all://*{hostname}"
        try:
            URLPattern(key)
        except Exception:
            log.warning("dropping unparseable no_proxy entry", entry=hostname)
            continue
        mounts[key] = None
    return mounts


def build_httpx_client(
    *,
    base_url: str | httpx.URL = "",
    timeout: Any = None,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
) -> httpx.Client:
    """Centralized ``httpx.Client`` construction (H26).

    Uses an explicit, sanitized proxy policy (see :func:`sanitized_proxy_map`)
    with ``trust_env=False`` so a hostile proxy environment can never crash
    client construction. Construction failure is logged and re-raised — callers
    must surface it as an error, never as a silent empty result.
    """
    proxy_map = sanitized_proxy_map()
    mounts: dict[str, httpx.BaseTransport | None] = {}
    for pattern, url in proxy_map.items():
        if url is None:
            # Bypass entry (NO_PROXY): None selects the default transport.
            mounts[pattern] = None
        else:
            mounts[pattern] = httpx.HTTPTransport(proxy=httpx.Proxy(url=url), trust_env=False)
    try:
        return httpx.Client(
            base_url=base_url,
            timeout=timeout if timeout is not None else get_settings().request_timeout_seconds,
            headers=headers,
            follow_redirects=follow_redirects,
            mounts=mounts or None,
            trust_env=False,
        )
    except Exception as exc:
        log.error("http_client_construction_failed", error=str(exc))
        raise


class HttpClient:
    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = base_url
        merged_headers = {"User-Agent": "serpent-hype-coin-engine/0.1"}
        if headers:
            merged_headers.update(headers)
        self._client = build_httpx_client(
            base_url=base_url,
            timeout=timeout,
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
