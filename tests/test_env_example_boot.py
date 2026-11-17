"""The shipped ``.env.example`` must boot as-is, with blanks parsed as unset."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from common.config import Settings, _allows_none

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

    assert blanks, "expected .env.example to contain blank placeholders"

    nullable_checks = 0
    for key in blanks:
        field_name = key.lower()
        field = Settings.model_fields.get(field_name)
        assert field is not None, key
        if not _allows_none(field.annotation):
            continue

        # Optional placeholders (numbers, secrets, URLs) are unset, not empty
        # strings or zero values that would read as configured.
        value = getattr(settings, field_name)
        assert value is None, f"{key} is blank in .env.example but parsed as {value!r}"
        nullable_checks += 1

    assert nullable_checks, "no blank .env.example keys map to nullable settings fields"
