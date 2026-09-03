"""End-to-end subprocess test of ``scripts/backfill_history.py``'s real __main__.

The rescore CLI is integration-testable only because ``SERPENT_DB_PATH`` /
``DATABASE_URL`` resolve into ``settings.database_url`` (single source of
truth in ``Settings``); backfill_history now gets the same treatment on the
*HTTP* side: ``COINGECKO_BASE_URL`` / ``DEFILLAMA_BASE_URL`` point the client
at a local mock, so the real write path — ``argparse → backfill_coingecko →
own session_scope() → insert_market_snapshot_once → commit`` — runs hermetically
against a throwaway DB while a seeded coin-id evidence row keeps ID resolution
off the live /search endpoint.

The test seeds a tradable pair + coingecko source + raw evidence carrying a
``coingecko_id``, drives the real ``__main__`` via a subprocess bound to the
override DB + mock URL, and asserts the committed ``market_snapshots`` rows:
exact count (the mock serves a fixed set of daily closes) and point-in-time
correctness (``observed_at == ts`` for every row).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from storage import models
from storage.database import Base
from storage.repository import (
    get_or_create_chain,
    get_or_create_source,
    store_raw_evidence,
    upsert_asset,
    upsert_pool_and_pair,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Mock CoinGecko daily closes: MOCK_DAYS distinct UTC-midnight timestamps so
# the (pair_id, ts, source_id) unique constraint never collides.
MOCK_DAYS = 5
MOCK_ANCHOR = datetime(2026, 6, 1, tzinfo=UTC)
MOCK_PRICES = [
    [
        int((MOCK_ANCHOR - timedelta(days=days_ago)).timestamp()) * 1000,
        round(1.0 + 0.05 * days_ago, 4),
    ]
    for days_ago in range(MOCK_DAYS, 0, -1)
]
MOCK_COIN_ID = "hype-fixture"


class _MarketChartHandler(BaseHTTPRequestHandler):
    """Serves CoinGecko-shaped ``/coins/{id}/market_chart`` from MOCK_PRICES."""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = urlparse(self.path).path
        if path.endswith("/market_chart"):
            body = json.dumps({"prices": MOCK_PRICES}).encode()
        else:  # /search fallback — not reached while the evidence row covers ID resolution
            body = json.dumps({"coins": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # silence request spam
        pass


@pytest.fixture()
def mock_coingecko() -> str:
    """Local CoinGecko stand-in on an OS-assigned free port (picked via 0)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MarketChartHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _seed_file_db(db_path: Path) -> tuple[int, int, int]:
    """Throwaway fleet DB: (pair_id, source_id, asset_id) of a tradable HYPE/USDC pair."""
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with Session() as session:
        chain = get_or_create_chain(
            session, "solana", name="Solana", vm_type="solana", native_symbol="SOL"
        )
        asset = upsert_asset(
            session,
            chain_id=chain.id,
            address="addr_hype",
            symbol="HYPE",
            name="Hype Fixture",
            first_seen_at=now - timedelta(days=200),
        )
        quote = upsert_asset(
            session,
            chain_id=chain.id,
            address="addr_usdc",
            symbol="USDC",
            name="USD Coin",
            first_seen_at=now - timedelta(days=400),
        )
        _, pair = upsert_pool_and_pair(
            session,
            chain_id=chain.id,
            dex_id="raydium",
            pair_address="pair_hype_usdc",
            base_asset_id=asset.id,
            quote_asset_id=quote.id,
            created_at_source=now - timedelta(days=200),
        )
        source = get_or_create_source(
            session,
            name="coingecko",
            source_type="market_data",
            tier="public_metadata",
            base_url="https://api.coingecko.com/api/v3",
        )
        # A coin-id evidence row so _resolve_coingecko_ids skips the live /search.
        store_raw_evidence(
            session,
            source=source,
            payload={"items": [{"symbol": "HYPE", "coingecko_id": MOCK_COIN_ID}]},
            observed_at=now - timedelta(days=1),
        )
        session.commit()
        return pair.id, source.id, asset.id


def _run_cli(db_path: Path, mock_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        # kill any ambient override so the run is hermetically pinned below
        "DATABASE_URL": "",
        "ENV": "local-single",
        "SERPENT_DB_PATH": str(db_path),
        "COINGECKO_BASE_URL": mock_url,
        "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "backfill_history.py"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
        env=env,
    )


def _snapshot_rows(db_path: Path, pair_id: int, source_id: int) -> list[tuple[datetime, datetime]]:
    """(ts, observed_at) of every market_snapshot for the pair/source combo."""
    table = models.MarketSnapshot.__table__
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return [
                (ts, observed_at)
                for ts, observed_at in conn.execute(
                    select(table.c.ts, table.c.observed_at)
                    .where(table.c.pair_id == pair_id, table.c.source_id == source_id)
                    .order_by(table.c.ts)
                )
            ]
    finally:
        engine.dispose()


def test_backfill_cli_commits_market_snapshots_via_real_main(
    tmp_path: Path, mock_coingecko: str
) -> None:
    """The real __main__ write path persists the mock's daily closes to the
    SERPENT_DB_PATH DB — closing the backfill write path end-to-end.

    Sanity: the seeded fleet starts with zero snapshots, so any rows after the
    run are provably the backfill's commit, and point-in-time correctness
    (observed_at == ts) must hold for every one of them.
    """
    db_path = tmp_path / "backfill.db"
    pair_id, source_id, _ = _seed_file_db(db_path)
    assert _snapshot_rows(db_path, pair_id, source_id) == []  # clean slate

    result = _run_cli(db_path, mock_coingecko, "--days", str(MOCK_DAYS), "--provider", "coingecko")

    assert result.returncode == 0, result.stderr
    # The script prints its result dict: provider, coverage, insert count, dry_run.
    parsed = next(
        ast.literal_eval(line)
        for line in result.stdout.splitlines()
        if line.startswith("{'provider'")
    )
    assert parsed["provider"] == "coingecko"
    assert parsed["assets_with_pairs"] == 1
    assert parsed["assets_covered"] == 1
    assert parsed["snapshots_inserted"] == MOCK_DAYS
    assert parsed["dry_run"] is False

    rows = _snapshot_rows(db_path, pair_id, source_id)
    assert len(rows) == MOCK_DAYS  # exactly the mock's daily closes were committed
    for ts, observed_at in rows:
        assert observed_at == ts, "backfilled snapshots must be point-in-time correct"
