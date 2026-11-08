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
