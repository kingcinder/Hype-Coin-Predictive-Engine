from __future__ import annotations

import json
import os

from trading import wallets


def test_load_wallets_reads_one_then_two_after_mtime(tmp_path) -> None:
    path = tmp_path / "wallets.json"
    path.write_text(
        json.dumps({"wallets": [{"address": "A" * 32, "label": "a", "chain": "solana"}]}),
        encoding="utf-8",
    )
    assert len(wallets.load_wallets(path)) == 1
    path.write_text(
        json.dumps(
            {
                "wallets": [
                    {"address": "A" * 32, "label": "a", "chain": "solana"},
                    {"address": "B" * 32, "label": "b", "chain": "solana"},
                ]
            }
        ),
        encoding="utf-8",
    )
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 3))
    assert len(wallets.load_wallets(path)) == 2


def test_invalid_solana_address_is_skipped(tmp_path, capsys) -> None:
    path = tmp_path / "wallets.json"
    path.write_text(
        json.dumps({"wallets": [{"address": "not-a-solana-address", "chain": "solana"}]}),
        encoding="utf-8",
    )
    assert wallets.load_wallets(path) == []
    assert "tracked_wallet_invalid" in capsys.readouterr().out
