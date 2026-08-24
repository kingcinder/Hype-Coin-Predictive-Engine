"""Probe and refresh the configured public RPC endpoint pools.

The command probes every effective endpoint for Solana, Base, and Ethereum,
then rewrites the corresponding ``*_RPC_POOL_CSV`` values with healthy
endpoints in their existing order. If a pruned URL was also the configured
chain primary, that primary is moved to the first surviving endpoint. The
default target is ``.env``; use
``--dry-run`` to inspect the proposed change without writing it.

Usage::

    python scripts/refresh_rpc_pools.py --dry-run
    python scripts/refresh_rpc_pools.py --env-file .env
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import Settings
from ingestion.source_clients import probe_rpc_endpoint

POOL_FIELDS: dict[str, str] = {
    "solana": "SOLANA_RPC_POOL_CSV",
    "base": "BASE_RPC_POOL_CSV",
    "ethereum": "ETHEREUM_RPC_POOL_CSV",
}
PRIMARY_FIELDS: dict[str, str] = {
    "solana": "SOLANA_RPC_URL",
    "base": "BASE_RPC_URL",
    "ethereum": "ETHEREUM_RPC_URL",
}


@dataclass(frozen=True)
class PoolProbeResult:
    chain: str
    configured: tuple[str, ...]
    healthy: tuple[str, ...]
    failed: tuple[str, ...]
    primary: str | None = None

    @property
    def healthy_configured(self) -> tuple[str, ...]:
        healthy = set(self.healthy)
        return tuple(url for url in self.configured if url in healthy)


def _split_csv(value: str) -> tuple[str, ...]:
    output: list[str] = []
    for part in value.split(","):
        url = part.strip()
        if url and url not in output:
            output.append(url)
    return tuple(output)


def probe_pools(
    settings: Settings,
    *,
    probe: Callable[[str, str], bool] | None = None,
) -> dict[str, PoolProbeResult]:
    """Probe each effective endpoint and return ordered health results."""
    check = probe or probe_rpc_endpoint
    results: dict[str, PoolProbeResult] = {}
    for chain, field in POOL_FIELDS.items():
        configured = _split_csv(str(getattr(settings, field.lower())))
        effective = tuple(settings.rpc_pool_endpoints(chain))
        healthy = tuple(url for url in effective if check(chain, url))
        healthy_set = set(healthy)
        results[chain] = PoolProbeResult(
            chain=chain,
            configured=configured,
            healthy=healthy,
            failed=tuple(url for url in effective if url not in healthy_set),
            primary=settings.rpc_url_for_chain(chain),
        )
    return results


def rewrite_env_pool_csvs(
    env_file: Path,
    results: dict[str, PoolProbeResult],
    *,
    dry_run: bool = False,
) -> str:
    """Rewrite pool assignments while preserving the rest of an env file.

    When a failed endpoint was also the configured chain primary, the primary
    URL is moved to the first surviving pool endpoint so configuration loading
    cannot immediately re-add the pruned endpoint.
    """
    content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    updated = content
    for chain, field in POOL_FIELDS.items():
        result = results[chain]
        value = ",".join(result.healthy_configured)
        pattern = re.compile(rf"^(\s*(?:export\s+)?{re.escape(field)}\s*=\s*).*$", re.MULTILINE)
        replacement = rf"\g<1>{value}"
        if pattern.search(updated):
            updated = pattern.sub(replacement, updated, count=1)
        else:
            if updated and not updated.endswith("\n"):
                updated += "\n"
            updated += f"{field}={value}\n"

        if (
            result.primary
            and result.primary in result.configured
            and result.primary not in result.healthy
            and result.healthy_configured
        ):
            primary_field = PRIMARY_FIELDS[chain]
            primary_pattern = re.compile(
                rf"^(\s*(?:export\s+)?{re.escape(primary_field)}\s*=\s*).*$",
                re.MULTILINE,
            )
            primary_replacement = rf"\g<1>{result.healthy_configured[0]}"
            if primary_pattern.search(updated):
                updated = primary_pattern.sub(primary_replacement, updated, count=1)
            else:
                if updated and not updated.endswith("\n"):
                    updated += "\n"
                updated += f"{primary_field}={result.healthy_configured[0]}\n"
    if not dry_run:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(updated, encoding="utf-8")
    return updated


def _settings_from_env_file(env_file: Path) -> Settings:
    if not env_file.exists():
        return Settings()
    values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").lower()
        values[key] = value.strip().strip("\"'")
    return Settings(**cast(Any, values))


def _print_results(results: dict[str, PoolProbeResult]) -> None:
    for chain, result in results.items():
        healthy = len(result.healthy)
        total = len(result.configured)
        print(f"{chain}: {healthy}/{total} effective endpoints healthy")
        for url in result.configured:
            status = "healthy" if url in result.healthy else "dead"
            print(f"  {status:7} {url}")
        for url in result.healthy:
            if url not in result.configured:
                print(f"  healthy {url} (effective primary; preserved outside CSV)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe every chain RPC endpoint and prune dead pool CSV entries."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(os.getenv("ENV_FILE", ".env")),
        help="env file to refresh (default: .env or ENV_FILE)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="probe and print the proposed file without writing it",
    )
    args = parser.parse_args(argv)

    settings = _settings_from_env_file(args.env_file)
    results = probe_pools(settings)
    _print_results(results)
    rewrite_env_pool_csvs(args.env_file, results, dry_run=args.dry_run)
    action = "Would update" if args.dry_run else "Updated"
    print(f"{action} {args.env_file}")
    return 0 if all(result.healthy for result in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
