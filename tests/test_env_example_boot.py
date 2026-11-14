"""The shipped ``.env.example`` must boot as-is, with blanks parsed as unset."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from common.config import Settings, secret_value

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _blank_keys(text: str) -> list[str]:
    keys: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not value.strip():
            keys.append(key.strip())
    return keys


@pytest.fixture
def example_settings(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    shutil.copyfile(EXAMPLE, env_file)
    blanks = _blank_keys(EXAMPLE.read_text(encoding="utf-8"))
    # The file's blank lines must reach validation; a developer shell with one
    # of these exported would otherwise mask the bug.
    for key in blanks:
        monkeypatch.delenv(key, raising=False)
    return Settings(_env_file=str(env_file)), blanks


def test_full_env_example_boots_with_blanks_unset(example_settings) -> None:
    settings, blanks = example_settings

    assert "TELEGRAM_API_ID" in blanks
    assert "LIVE_MAX_BANKROLL_SOL" in blanks
    assert "LIVE_DAILY_LOSS_LIMIT_SOL" in blanks
    assert "HELIUS_API_KEY" in blanks

    for key in blanks:
        assert key.lower() in Settings.model_fields, key

    # Optional placeholders (numbers, secrets, URLs) are unset, not empty strings
    # or zero values that would read as configured.
    assert settings.telegram_api_id is None
    assert settings.live_max_bankroll_sol is None
    assert settings.live_daily_loss_limit_sol is None
    assert settings.engine_api_token is None
    assert settings.helius_api_key is None
    assert settings.alchemy_api_key is None
    assert settings.etherscan_api_key is None
    assert settings.birdeye_api_key is None
    assert settings.cmc_api_key is None
    assert settings.gmgn_api_key is None
    assert settings.gmgn_signer_secret is None
    assert settings.reddit_client_secret is None
    assert settings.reddit_client_id is None
    assert settings.llm_api_key is None
    assert settings.llm_base_url is None
    assert settings.quicknode_evm_rpc_url is None
    assert settings.webhook_url_allowlist_csv is None
    assert settings.gmgn_wallet_address is None
    assert settings.telegram_api_hash is None
    assert secret_value(settings.helius_api_key) is None
