"""Deployment-secret guards for the production Compose overlay."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_prod_compose_requires_a_non_empty_birdeye_key() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    engine_service = compose.partition("\n  ollama:")[0]
    # Compose's ``:?`` form aborts ``up`` when the variable is unset or empty,
    # so production cannot deploy with the Birdeye feed silently disabled.
    assert "BIRDEYE_API_KEY: ${BIRDEYE_API_KEY:?" in engine_service


def test_prod_env_file_is_gitignored() -> None:
    ignored = {
        line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert ".env.prod" in ignored
