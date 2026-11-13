#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Install the dev/test dependency set — the one install path CI, the
# Makefile, the desktop installer and start-engine.sh all share.
#
#   scripts/install_dev_deps.sh              # uses `python -m pip`
#   scripts/install_dev_deps.sh .venv/bin/pip
#
# requirements-dev.txt is the pinned lock and is authoritative: it is
# installed first, and the project goes in editable with --no-deps, so pip
# never re-resolves the dependencies from the pyproject floors. That is how
# SQLAlchemy 2.1 arrived unannounced and turned the mypy gate red; the
# floors stay as the compatibility range for end users, while this path
# stays on the versions the gate was verified against.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PIP=("python" "-m" "pip")
if [ "$#" -gt 0 ]; then
    PIP=("$@")
fi

"${PIP[@]}" install --upgrade pip
"${PIP[@]}" install -r requirements-dev.txt
"${PIP[@]}" install -e . --no-deps

# `--no-deps` above means pip will not notice a lock that is missing a declared
# dependency (defusedxml was missing once), so verify the environment instead.
"${PIP[@]}" check

echo "installed: $("${PIP[@]}" freeze | grep -Ei '^(sqlalchemy|mypy|pytest|ruff)==' | paste -sd ' ' -)"
