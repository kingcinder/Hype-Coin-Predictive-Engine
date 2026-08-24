from __future__ import annotations

from common.config import Settings
from scripts.refresh_rpc_pools import (
    PoolProbeResult,
    probe_pools,
    rewrite_env_pool_csvs,
)


def test_probe_pools_keeps_only_healthy_configured_endpoints() -> None:
    settings = Settings(
        solana_rpc_url="https://sol-good.example",
        base_rpc_url="https://base-good.example",
        ethereum_rpc_url="https://eth-good.example",
        solana_rpc_pool_csv="https://sol-good.example,https://sol-dead.example",
        base_rpc_pool_csv="https://base-dead.example,https://base-good.example",
        ethereum_rpc_pool_csv="https://eth-good.example,https://eth-dead.example",
    )

    def probe(_chain: str, url: str) -> bool:
        return "dead" not in url

    results = probe_pools(settings, probe=probe)
    assert results["solana"].healthy_configured == ("https://sol-good.example",)
    assert results["base"].healthy_configured == ("https://base-good.example",)
    assert results["ethereum"].healthy_configured == ("https://eth-good.example",)
    assert "https://sol-dead.example" in results["solana"].failed


def test_rewrite_env_pool_csvs_preserves_other_settings_and_order(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LOG_LEVEL=INFO\n"
        "BASE_RPC_URL=https://dead.example\n"
        "BASE_RPC_POOL_CSV=https://dead.example,https://good.example\n"
        "SOLANA_RPC_POOL_CSV=old\n",
        encoding="utf-8",
    )
    results = {
        chain: PoolProbeResult(chain, (), (), ()) for chain in ("solana", "ethereum")
    }
    results["solana"] = PoolProbeResult("solana", ("sol-new",), ("sol-new",), ())
    results["base"] = PoolProbeResult(
        "base",
        ("https://dead.example", "https://good.example"),
        ("https://good.example",),
        (),
        primary="https://dead.example",
    )
    results["ethereum"] = PoolProbeResult("ethereum", ("eth-new",), ("eth-new",), ())

    rewrite_env_pool_csvs(env_file, results)
    content = env_file.read_text(encoding="utf-8")
    assert "LOG_LEVEL=INFO" in content
    assert "BASE_RPC_URL=https://good.example" in content
    assert "BASE_RPC_POOL_CSV=https://good.example" in content
    assert "SOLANA_RPC_POOL_CSV=sol-new" in content
    assert "ETHEREUM_RPC_POOL_CSV=eth-new" in content


def test_rewrite_env_pool_csvs_dry_run_does_not_write(tmp_path) -> None:
    env_file = tmp_path / ".env"
    original = "BASE_RPC_POOL_CSV=https://dead.example\n"
    env_file.write_text(original, encoding="utf-8")
    results = {
        "solana": PoolProbeResult("solana", (), (), ()),
        "base": PoolProbeResult(
            "base", ("https://dead.example", "https://good.example"), ("https://good.example",), ()
        ),
        "ethereum": PoolProbeResult("ethereum", (), (), ()),
    }

    rendered = rewrite_env_pool_csvs(env_file, results, dry_run=True)
    assert "BASE_RPC_POOL_CSV=https://good.example" in rendered
    assert env_file.read_text(encoding="utf-8") == original
