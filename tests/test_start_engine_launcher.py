"""start-engine.sh must probe /health with the bearer token from .env (F3).

The API gates /health too, so an unauthenticated probe never succeeds. The
launcher's helper reads ENGINE_API_TOKEN from .env and opens the GUI once an
authenticated probe passes. External commands are stubbed so no engine starts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOKEN = "launcher-test-token-0123456789abcdef"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_gui_opens_after_authenticated_health_probe(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "activate").write_text("", encoding="utf-8")
    (project / ".env").write_text(f"ENGINE_API_TOKEN={TOKEN}\n", encoding="utf-8")
    shutil.copy(REPO / "start-engine.sh", project / "start-engine.sh")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    opened = tmp_path / "opened.txt"
    seen = tmp_path / "curl-args.txt"
    seen_cfg = tmp_path / "curl-config.txt"
    # curl succeeds only when the bearer token from .env arrives
    # on stdin: the launcher pipes it in via curl -K - so the token
    # never appears in argv (and thus never in the process list).
    _stub(
        stubs / "curl",
        f'echo "$@" >> "{seen}"\n'
        'cfg="$(cat)"\n'
        f'echo "$cfg" >> "{seen_cfg}"\n'
        f'case "$cfg" in *"Authorization: Bearer {TOKEN}"*) exit 0;; esac\n'
        "exit 22\n",
    )
    _stub(stubs / "xdg-open", f'echo "$1" >> "{opened}"\n')
    _stub(stubs / "python", "sleep 4\nexit 0\n")

    env = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "SERPENT_PROJECT_DIR": str(project),
        # Explicit PID file: a missing file means no engine, so this test does
        # not depend on whether the host is running one.
        "SERPENT_ENGINE_PID_FILE": str(tmp_path / "none.pid"),
    }
    # Keep the probe on the .env path: an ambient ENGINE_API_TOKEN
    # (e.g. leaked into os.environ by the provisioning tests) would
    # otherwise take precedence over the .env this test sets up.
    env.pop("ENGINE_API_TOKEN", None)
    result = subprocess.run(
        ["bash", str(project / "start-engine.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    # The helper runs in the background; wait for it to call xdg-open.
    deadline = time.monotonic() + 20
    while not opened.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert opened.exists(), "GUI was never opened"
    assert opened.read_text(encoding="utf-8").strip() == "http://localhost:8501"
    # The bearer header arrived through curl's stdin config (curl -K -) ...
    assert f'header = "Authorization: Bearer {TOKEN}"' in seen_cfg.read_text(encoding="utf-8")
    # ... and the token never appeared in curl's argv (the process list).
    assert TOKEN not in seen.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_launcher_prunes_old_backups_to_the_configured_keep(tmp_path: Path) -> None:
    """A launch keeps the newest SERPENT_BACKUP_KEEP snapshots and drops the rest.

    Every launch writes a fresh snapshot, so an unbounded backup directory was a
    slow disk leak. This drives the real launcher (externals stubbed) with three
    pre-existing snapshots and ``SERPENT_BACKUP_KEEP=2``: the new backup plus one
    old snapshot must survive, the other two must be gone.
    """
    import sqlite3

    project = tmp_path / "project"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "activate").write_text("", encoding="utf-8")
    (project / ".env").write_text("", encoding="utf-8")
    (project / "scripts").mkdir()
    shutil.copy(REPO / "start-engine.sh", project / "start-engine.sh")
    shutil.copy(REPO / "scripts" / "prune_backups.py", project / "scripts" / "prune_backups.py")
    (project / "serpent.db").write_bytes(b"")
    with sqlite3.connect(project / "serpent.db") as con:
        con.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
        con.execute("INSERT INTO marker (id) VALUES (1)")

    backups = tmp_path / "backups"
    backups.mkdir()
    for day in (1, 2, 3):
        (backups / f"serpent-2026010{day}-010000.db").write_bytes(b"old")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs / "curl", "exit 0\n")
    _stub(stubs / "xdg-open", "exit 0\n")
    _stub(stubs / "python", "exit 0\n")

    env = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "SERPENT_PROJECT_DIR": str(project),
        "SERPENT_ENGINE_PID_FILE": str(tmp_path / "none.pid"),
        "SERPENT_BACKUP_DIR": str(backups),
        "SERPENT_BACKUP_KEEP": "2",
        "SERPENT_LOG_DIR": str(tmp_path / "logs"),
    }
    result = subprocess.run(
        ["bash", str(project / "start-engine.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    remaining = sorted(path.name for path in backups.iterdir())
    # Four snapshots existed once this launch wrote its own; the newest two are
    # the new backup and the newest pre-existing one.
    assert remaining == ["serpent-20260103-010000.db", remaining[-1]], remaining
    assert len(remaining) == 2, remaining
    assert remaining[-1] not in {"serpent-20260101-010000.db", "serpent-20260102-010000.db"}
    assert "backups: kept 2 newest of 4" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_launcher_rejects_a_non_numeric_keep(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(REPO / "start-engine.sh", project / "start-engine.sh")

    env = {
        **os.environ,
        "SERPENT_PROJECT_DIR": str(project),
        "SERPENT_BACKUP_KEEP": "lots",
    }
    result = subprocess.run(
        ["bash", str(project / "start-engine.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "SERPENT_BACKUP_KEEP must be a positive integer" in result.stderr
