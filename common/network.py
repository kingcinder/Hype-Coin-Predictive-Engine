from __future__ import annotations

from ipaddress import ip_address

from common.config import Settings
from common.logging import get_logger

log = get_logger(__name__)


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def validate_network_exposure(settings: Settings) -> None:
    """Require explicit, authenticated configuration for non-loopback binds."""
    exposed = {
        name: host
        for name, host in (("api", settings.api_host), ("ui", settings.ui_host))
        if not _is_loopback(host)
    }
    if not exposed:
        return

    log.warning(
        "non_loopback_service_bind",
        api_host=settings.api_host,
        ui_host=settings.ui_host,
        engine_allow_lan=settings.engine_allow_lan,
        message="API/UI listeners are reachable beyond loopback",
    )
    if not settings.engine_allow_lan:
        raise RuntimeError("non-loopback binds require ENGINE_ALLOW_LAN=1")
    if not (settings.engine_api_token or "").strip():
        raise RuntimeError("non-loopback binds require ENGINE_API_TOKEN")
    if settings.engine_api_auth_bypass:
        raise RuntimeError("ENGINE_API_AUTH_BYPASS must be disabled for non-loopback binds")
