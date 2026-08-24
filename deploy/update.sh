#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-/opt/serpent}"
BRANCH="${BRANCH:-main}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (for example: sudo $0)" >&2
  exit 1
fi
[[ -d "$PREFIX/.git" ]] || { echo "Not an installed checkout: $PREFIX" >&2; exit 1; }

cd "$PREFIX"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"
"$PREFIX/.venv/bin/pip" install --upgrade "$PREFIX"
"$PREFIX/.venv/bin/alembic" -c storage/alembic.ini upgrade head
systemctl daemon-reload
systemctl restart serpent-api.service serpent-worker.service serpent-ui.service
systemctl try-restart serpent-retention.timer >/dev/null 2>&1 || true
systemctl --no-pager --full status serpent-api.service serpent-worker.service serpent-ui.service serpent-retention.timer
