"""Secret scan test (Phase D5).

Walks every tracked text file (git ls-files; skip binary, skip .env)
and fails if any line matches:
  (a) [1-9A-HJ-NP-Za-km-z]{80,}  (a base58 secret-key-length token)
  (b) (?i)(seed phrase|mnemonic)\s*[:=]\s*["'][^"']{10,}["']
  (c) (?i)private[_ ]?key\s*[:=]\s*["'][^"']{20,}["']
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def find_secret_tokens():
    """Find files with potential secrets in git-tracked text files."""
    # Get list of tracked text files, skip .env
    result = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        cwd=".",
    )
    tracked_files = result.stdout.strip().split("\n")

    # Filter to text files, skip .env
    text_files = []
    for f in tracked_files:
        if not f or f.endswith(".env") or f.endswith(".env.local"):
            continue
        # Skip binary-like extensions
        if f.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".tar", ".gz")):
            continue
        text_files.append(f)

    patterns = [
        # (a) base58 secret key length token
        re.compile(r"[1-9A-HJ-NP-Za-km-z]{80,}"),
        # (b) seed phrase or mnemonic
        re.compile(r"(?i)(seed phrase|mnemonic)\s*[:=]\s*[\"'][^\"']{10,}['\"]"),
        # (c) private key
        re.compile(r"(?i)private[_ ]?key\s*[:=]\s*[\"'][^\"']{20,}['\"]"),
    ]

    hits = []
    for f in text_files:
        try:
            content = Path(f).read_text(errors="replace")
        except Exception:
            continue
        for i, line in enumerate(content.split("\n"), 1):
            for pattern in patterns:
                if pattern.search(line):
                    hits.append(
                        {
                            "file": f,
                            "line": i,
                            "match": line.strip(),
                            "pattern": pattern.pattern,
                        }
                    )
                    break  # only one hit per line needed

    return hits


def test_no_secrets_in_repo():
    """Test that no secrets are found in the repo."""
    hits = find_secret_tokens()
    if hits:
        print(f"\n=== SECRET HITS FOUND ({len(hits)}) ===")
        for h in hits:
            print(f"  File: {h['file']}, Line {h['line']}: {h['match']}")
        print("=========================================\n")
        pytest.fail(f"Found {len(hits)} potential secrets in the repository")


if __name__ == "__main__":
    # Allow running standalone
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
