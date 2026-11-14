"""Credentials are SecretStr and stay out of repr/dumps/fingerprints (C1)."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from common.config import get_settings, secret_value
from storage.repository import _config_fingerprint_input

_SECRET_ENV = {
    "ENGINE_API_TOKEN": "MARKER-engine-token",
    "GMGN_API_KEY": "MARKER-gmgn-key",
    "GMGN_SIGNER_SECRET": "MARKER-gmgn-secret",
    "HELIUS_API_KEY": "MARKER-helius",
    "ALCHEMY_API_KEY": "MARKER-alchemy",
    "ETHERSCAN_API_KEY": "MARKER-etherscan",
    "REDDIT_CLIENT_SECRET": "MARKER-reddit",
    "CMC_API_KEY": "MARKER-cmc",
    "GITHUB_TOKEN": "MARKER-github",
    "MINIO_SECRET_KEY": "MARKER-minio",
    "BIRDEYE_API_KEY": "MARKER-birdeye",
}


@pytest.fixture
def secret_settings(monkeypatch):
    for name, value in _SECRET_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_credentials_are_secretstr(secret_settings) -> None:
    for field in (
        "engine_api_token",
        "gmgn_api_key",
        "gmgn_signer_secret",
        "helius_api_key",
        "alchemy_api_key",
        "etherscan_api_key",
        "reddit_client_secret",
        "cmc_api_key",
        "github_token",
        "minio_secret_key",
        "birdeye_api_key",
    ):
        assert isinstance(getattr(secret_settings, field), SecretStr), field
    assert secret_value(secret_settings.gmgn_api_key) == "MARKER-gmgn-key"


def test_credentials_absent_from_repr_and_dumps(secret_settings) -> None:
    text = repr(secret_settings) + str(secret_settings)
    dumped = json.dumps(secret_settings.model_dump(mode="json"), default=str)
    for marker in _SECRET_ENV.values():
        assert marker not in text
        assert marker not in dumped


def test_credentials_absent_from_config_fingerprint(secret_settings) -> None:
    payload = _config_fingerprint_input()
    decoded = json.loads(payload)
    for marker in _SECRET_ENV.values():
        assert marker not in payload
    for name in (
        "engine_api_token",
        "gmgn_api_key",
        "gmgn_signer_secret",
        "cmc_api_key",
        "minio_secret_key",
        "birdeye_api_key",
    ):
        assert name not in decoded
