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


def test_birdeye_production_requirement_is_documented() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    secrets = (ROOT / "docs" / "secrets.md").read_text(encoding="utf-8")

    example_flat = " ".join(example.lower().split())
    production_secrets = secrets.partition("## Production secret storage")[2]
    production_secrets = production_secrets.partition("\n## ")[0].replace("`", "")
    secrets_flat = " ".join(production_secrets.lower().split())

    # The example must carry the key and mark it as production-required, not
    # merely mention the name somewhere in the template.
    assert "birdeye_api_key=" in example_flat
    assert "required in production" in example_flat
    # The runbook's production-secret section must make the requirement explicit.
    assert "required birdeye_api_key" in secrets_flat


def test_prod_env_file_is_gitignored() -> None:
    ignored = {
        line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert ".env.prod" in ignored
