"""Regression tests for script-level hardening (REVIEW.md H10, M17, M18, M23, L1).

- H10: ``refresh_rpc_pools --apply`` no longer writes an empty CSV line for a
  chain whose probes all failed — an all-down probe result leaves the chain's
  existing configuration untouched.
- M17: ``seed_fixtures`` is idempotent — a second run adds no duplicate
  Holder / ContractFlag / fixture_seed health rows.
- M18: dry-run backfills report zero inserts (no phantom counts), and the CLI
  exits 1 when nothing resolved and errors occurred.
- M23: ``dev.sh clean-db`` requires confirmation (or ``--force``).
- L1: the backup sidecar shares the compactor's merge-lock filename and the
  two lock implementations are mutually exclusive.
- rescore: a bare write invocation requires an explicit ``yes`` (exit 2 on
  EOF); ``--yes`` bypasses the prompt.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / relative_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── H10 ──────────────────────────────────────────────────────────────────────

def test_rewrite_env_pool_csvs_leaves_all_down_chain_untouched(tmp_path) -> None:
    from scripts.refresh_rpc_pools import PoolProbeResult, rewrite_env_pool_csvs

    env_file = tmp_path / ".env"
    env_file.write_text(
        "SOLANA_RPC_POOL_CSV=https://sol-a.example,https://sol-b.example\n"
        "BASE_RPC_POOL_CSV=https://base.example\n",
        encoding="utf-8",
    )
    results = {
        "solana": PoolProbeResult(
            chain="solana",
            configured=("https://sol-a.example", "https://sol-b.example"),
            healthy=(),
            failed=("https://sol-a.example", "https://sol-b.example"),
        ),
        "base": PoolProbeResult(
            chain="base",
            configured=("https://base.example",),
            healthy=("https://base.example",),
            failed=(),
        ),
        "ethereum": PoolProbeResult(
            chain="ethereum", configured=(), healthy=(), failed=()
        ),
    }
    rewrite_env_pool_csvs(env_file, results)
    content = env_file.read_text(encoding="utf-8")
    # All-down solana keeps its existing CSV; nothing is appended for the
    # unconfigured ethereum chain either.
    assert "SOLANA_RPC_POOL_CSV=https://sol-a.example,https://sol-b.example" in content
    assert "BASE_RPC_POOL_CSV=https://base.example" in content
    assert "ETHEREUM_RPC_POOL_CSV=" not in content


# ── M18 ──────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    """Stand-in for httpx.Client serving fixed market_chart closes."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get(self, url: str, params: dict | None = None, timeout: float = 30.0):
        anchor = datetime(2026, 6, 1, tzinfo=UTC)
        prices = [
            [int((anchor - timedelta(days=d)).timestamp()) * 1000, 1.0 + 0.01 * d]
            for d in range(5, 0, -1)
        ]
        return _FakeResponse({"prices": prices})

    def close(self) -> None:
        pass


def _seed_backfill_pair(session):
    from storage.repository import (
        get_or_create_chain,
        get_or_create_source,
        store_raw_evidence,
        upsert_asset,
        upsert_pool_and_pair,
    )

    now = datetime(2026, 1, 1, tzinfo=UTC)
    chain = get_or_create_chain(
        session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
    )
    asset = upsert_asset(
        session, chain_id=chain.id, address="addr_hype", symbol="HYPE",
        name="Hype Fixture", first_seen_at=now - timedelta(days=200),
    )
    quote = upsert_asset(
        session, chain_id=chain.id, address="addr_usdc", symbol="USDC",
        name="USD Coin", first_seen_at=now - timedelta(days=400),
    )
    _, pair = upsert_pool_and_pair(
        session, chain_id=chain.id, dex_id="raydium",
        pair_address="pair_hype_usdc", base_asset_id=asset.id,
        quote_asset_id=quote.id, created_at_source=now - timedelta(days=200),
    )
    source = get_or_create_source(
        session, name="coingecko", source_type="market_data",
        tier="public_metadata", base_url="https://api.coingecko.com/api/v3",
    )
    # A coin-id evidence row keeps ID resolution off the live /search endpoint.
    store_raw_evidence(
        session, source=source,
        payload={"items": [{"symbol": "HYPE", "coingecko_id": "hype-fixture"}]},
        observed_at=now - timedelta(days=1),
    )
    session.commit()
    return pair, source


def test_backfill_dry_run_reports_zero_inserts(session, monkeypatch) -> None:
    """M18: a dry run must not report phantom inserted rows."""
    import scripts.backfill_history as bh

    _seed_backfill_pair(session)
    monkeypatch.setattr(bh.httpx, "Client", _FakeHttpClient)
    result = bh.backfill_coingecko(session=session, days=7, dry_run=True)
    assert result["dry_run"] is True
    assert result["assets_covered"] == 1
    assert result["snapshots_inserted"] == 0
    # And a real (non-dry) run against the same fake inserts the closes.
    result = bh.backfill_coingecko(session=session, days=7, dry_run=False)
    assert result["snapshots_inserted"] == 5


def test_backfill_main_exit_code_on_total_failure(session, monkeypatch, capsys) -> None:
    """M18: exit 1 when nothing resolved and errors occurred; 0 otherwise."""
    import scripts.backfill_history as bh

    monkeypatch.setattr(
        bh,
        "backfill_coingecko",
        lambda *a, **k: {
            "provider": "coingecko",
            "assets_with_pairs": 2,
            "assets_covered": 0,
            "snapshots_inserted": 0,
            "resolve_errors": 2,
            "dry_run": False,
        },
    )
    assert bh.main(["--provider", "coingecko", "--days", "7"]) == 1

    monkeypatch.setattr(
        bh,
        "backfill_coingecko",
        lambda *a, **k: {
            "provider": "coingecko",
            "assets_with_pairs": 2,
            "assets_covered": 0,
            "snapshots_inserted": 0,
            "dry_run": True,
        },
    )
    assert bh.main(["--provider", "coingecko", "--days", "7", "--dry-run"]) == 0


# ── M17 ──────────────────────────────────────────────────────────────────────

def test_seed_fixtures_is_idempotent(session) -> None:
    """M17: a second seed run adds no duplicate Holder/ContractFlag rows and
    leaves exactly one fixture_seed health marker."""
    from scripts import seed_fixtures as sf
    from storage import models

    sf.seed_fixture_data(session=session)
    session.commit()

    def counts():
        return {
            "holders": session.scalar(select(func.count()).select_from(models.Holder)),
            "flags": session.scalar(select(func.count()).select_from(models.ContractFlag)),
            "markers": session.scalar(
                select(func.count())
                .select_from(models.SystemHealth)
                .where(models.SystemHealth.component == "fixture_seed")
            ),
        }

    first = counts()
    assert first["markers"] == 1

    sf.seed_fixture_data(session=session)
    session.commit()
    assert counts() == first


# ── M23 ──────────────────────────────────────────────────────────────────────

def _run_clean_db_function(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real ``cmd_clean_db`` function from scripts/dev.sh in a throwaway
    cwd. (dev.sh ``cd``s to the repo root on startup, so invoking the whole
    script with --force would target the repo's own serpent.db — extracting
    the function keeps the test hermetic while still testing the shipped code.)
    """
    script = REPO_ROOT / "scripts" / "dev.sh"
    func_text = subprocess.run(
        ["sed", "-n", "/^cmd_clean_db() {/,/^}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    assert func_text.startswith("cmd_clean_db() {"), "function not found in dev.sh"
    quoted = " ".join(f"'{a}'" for a in args)
    return subprocess.run(
        ["bash", "-c", f"{func_text}\ncd \"$1\" && cmd_clean_db {quoted}", "_", str(tmp_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_dev_sh_clean_db_requires_confirmation(tmp_path) -> None:
    """M23: a bare clean-db aborts on EOF instead of deleting the database."""
    db = tmp_path / "serpent.db"
    db.write_bytes(b"data")
    result = _run_clean_db_function(tmp_path)
    assert db.exists(), "bare clean-db must not delete without confirmation"
    assert "Aborted" in result.stdout


def test_dev_sh_clean_db_force_deletes(tmp_path) -> None:
    db = tmp_path / "serpent.db"
    db.write_bytes(b"data")
    result = _run_clean_db_function(tmp_path, "--force")
    assert result.returncode == 0, result.stderr
    assert not db.exists()


# ── rescore confirmation ─────────────────────────────────────────────────────

def _rescore_env(db_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "ENV": "local-single",
        "SERPENT_DB_PATH": str(db_path),
        "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }


def test_rescore_bare_write_requires_explicit_yes(tmp_path) -> None:
    """A bare write invocation with EOF on stdin aborts with exit 2."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "rescore.py")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=_rescore_env(tmp_path / "rescore.db"),
    )
    assert result.returncode == 2
    assert "Aborted" in result.stdout


def test_rescore_yes_bypasses_prompt(tmp_path) -> None:
    """--yes skips the confirmation prompt (the run then proceeds to rescore)."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "rescore.py"), "--yes"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=_rescore_env(tmp_path / "rescore.db"),
    )
    assert "Type 'yes' to continue" not in result.stdout


# ── L1 ───────────────────────────────────────────────────────────────────────

def test_backup_and_compactor_share_merge_lock_name() -> None:
    """L1: the backup sidecar quiesces on the same lock file as the compactor."""
    from ops.archive import ARCHIVE_MERGE_LOCK_NAME

    backup = _load_script_module("backup", "scripts/backup.py")
    assert backup._ARCHIVE_MERGE_LOCK_NAME == ARCHIVE_MERGE_LOCK_NAME  # noqa: SLF001


def test_backup_lock_is_mutually_exclusive_with_compactor_lock(tmp_path, monkeypatch) -> None:
    """While the compactor holds its merge lock, the backup sidecar's lock
    acquisition times out (and vice versa) — the tar quiesce actually works."""
    import ops.archive as archive_mod

    backup = _load_script_module("backup", "scripts/backup.py")
    monkeypatch.setattr(backup, "ARCHIVE_DIR", tmp_path)

    acquired: list[bool] = []
    release = threading.Event()

    def hold_compactor_lock() -> None:
        with archive_mod.archive_merge_lock(tmp_path):
            acquired.append(True)
            release.wait(timeout=10.0)

    holder = threading.Thread(target=hold_compactor_lock, daemon=True)
    holder.start()
    # Wait until the holder really owns the lock: without this the main
    # thread could win the race and the test would assert nothing.
    deadline = time.monotonic() + 10.0
    while not acquired and time.monotonic() < deadline:
        time.sleep(0.01)
    assert acquired, "holder thread never acquired the compactor lock"
    try:
        with backup._archive_merge_lock(timeout=2):  # noqa: SLF001
            pytest.fail("backup lock acquired while compactor holds it")
    except TimeoutError:
        pass
    finally:
        release.set()
        holder.join(timeout=10.0)


class _FailingHttpClient:
    """Stand-in for httpx.Client whose every request fails (total outage)."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get(self, url: str, timeout: float = 30.0):
        raise RuntimeError("network is down")

    def close(self) -> None:
        pass


def test_backfill_defillama_counts_failed_days_as_resolve_errors(session, monkeypatch) -> None:
    """M18: the DeFiLlama path must populate resolve_errors like CoinGecko —
    a fully-failed backfill must be distinguishable from a successful one."""
    import scripts.backfill_history as bh

    _seed_backfill_pair(session)
    monkeypatch.setattr(bh.httpx, "Client", _FailingHttpClient)
    result = bh.backfill_defillama(session=session, days=3, dry_run=True)
    assert result["assets_covered"] == 0
    assert result["snapshots_inserted"] == 0
    assert result["resolve_errors"] == 3


def test_backfill_main_exit_code_defillama_total_failure(monkeypatch, capsys) -> None:
    """M18: the CLI exits 1 for a fully-failed DeFiLlama backfill too."""
    import scripts.backfill_history as bh

    monkeypatch.setattr(
        bh,
        "backfill_defillama",
        lambda *a, **k: {
            "provider": "defillama",
            "assets_with_pairs": 1,
            "assets_covered": 0,
            "snapshots_inserted": 0,
            "resolve_errors": 3,
            "dry_run": False,
        },
    )
    assert bh.main(["--provider", "defillama", "--days", "3"]) == 1
