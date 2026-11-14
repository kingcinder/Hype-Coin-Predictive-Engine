from __future__ import annotations

import logging
import sys

import structlog

from common.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    # httpx logs "HTTP Request: GET <url>" at INFO, and provider URLs carry
    # API keys (Helius api-key query, Alchemy /v2/<key>, Etherscan apikey).
    # Keep request-line logging off the default INFO surface.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)
