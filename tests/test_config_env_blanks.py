"""Blank placeholder env values must not abort Settings parsing."""

from __future__ import annotations

from common.config import Settings, secret_value


def test_blank_telegram_api_id_is_treated_as_unset(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    settings = Settings(_env_file=None)
    assert settings.telegram_api_id is None


def test_telegram_api_hash_is_a_secret(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_HASH", "0" * 32)
    settings = Settings(_env_file=None)
    assert secret_value(settings.telegram_api_hash) == "0" * 32
    assert "0" * 32 not in repr(settings)
