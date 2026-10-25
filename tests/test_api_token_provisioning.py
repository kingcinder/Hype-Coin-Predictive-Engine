"""First-run provisioning of ENGINE_API_TOKEN (F2).

The bearer gate fails closed when no token is configured. ``ensure_api_token``
generates a random token once, writes it to the project ``.env`` with mode
0600, and never emits the value to logs.
"""

from __future__ import annotations

import os
import stat

import pytest

from common.config import ensure_api_token


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    monkeypatch.delenv("ENGINE_API_TOKEN", raising=False)
    monkeypatch.delenv("ENGINE_API_AUTH_BYPASS", raising=False)
    yield
    os.environ.pop("ENGINE_API_TOKEN", None)


def test_generates_token_once_and_writes_env_0600(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ENV\n", encoding="utf-8")

    token = ensure_api_token(env_file)

    assert token and len(token) >= 32
    text = env_file.read_text(encoding="utf-8")
    assert f"ENGINE_API_TOKEN={token}" in text
    assert "ENV\n" in text  # unrelated lines preserved
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert os.environ["ENGINE_API_TOKEN"] == token

    # Second call reuses the stored token instead of rotating it.
    monkeypatch.delenv("ENGINE_API_TOKEN", raising=False)
    again = ensure_api_token(env_file)
    assert again == token
    assert env_file.read_text(encoding="utf-8").count("ENGINE_API_TOKEN=") == 1


def test_fills_empty_placeholder_line(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ENGINE_API_TOKEN=\nENGINE_API_AUTH_BYPASS=0\n", encoding="utf-8")

    token = ensure_api_token(env_file)

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines.count(f"ENGINE_API_TOKEN={token}") == 1
    assert "ENGINE_API_AUTH_BYPASS=0" in lines
    assert not any(line == "ENGINE_API_TOKEN=" for line in lines)


def test_token_is_not_logged(tmp_path, capsys):
    env_file = tmp_path / ".env"
    token = ensure_api_token(env_file)
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err


def test_skipped_when_bypass_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_API_AUTH_BYPASS", "1")
    env_file = tmp_path / ".env"

    assert ensure_api_token(env_file) is None
    assert not env_file.exists()


def test_existing_token_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_API_TOKEN", "operator-supplied-token-value-1234")
    env_file = tmp_path / ".env"

    assert ensure_api_token(env_file) == "operator-supplied-token-value-1234"
    assert not env_file.exists()
