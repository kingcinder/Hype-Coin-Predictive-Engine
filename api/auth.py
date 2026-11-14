"""Authentication dependencies for FastAPI routes."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from storage.database import get_session


def _check_bearer_token(
    authorization: str | None, settings: Settings | None = None
) -> tuple[int, str] | None:
    """Validate the ``Authorization: Bearer <token>`` credential (H4).

    Returns ``None`` when the request is authorized, else ``(status, detail)``.
    Fail closed: when no token is configured and the local-dev bypass is off,
    every request is refused with 403. Comparison is constant-time.
    """
    settings = settings or get_settings()
    if settings.engine_api_auth_bypass:
        return None
    token = (settings.engine_api_token or "").strip()
    if not token:
        return (
            403,
            "API token not configured: set ENGINE_API_TOKEN "
            "(or ENGINE_API_AUTH_BYPASS=1 for local development only)",
        )
    if not authorization:
        return 401, "missing Authorization header: expected 'Bearer <token>'"
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return 401, "malformed Authorization header: expected 'Bearer <token>'"
    if not hmac.compare_digest(credential, token):
        return 401, "invalid API token"
    return None


DbSession = Annotated[Session, Depends(get_session)]
"""SQLAlchemy session dependency for DB access."""


def check_bearer_token(
    authorization: str | None, settings: Settings | None = None
) -> tuple[int, str] | None:
    """Public wrapper for bearer-token validation."""
    return _check_bearer_token(authorization, settings=settings)
