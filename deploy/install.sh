#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-/opt/serpent}"
SERVICE_USER="${SERVICE_USER:-serpent}"
REPO_URL="${REPO_URL:-}"
BRANCH="${BRANCH:-main}"
ENV_FILE="${ENV_FILE:-$PREFIX/.env}"
UNIT_DIR="/etc/systemd/system"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (for example: sudo $0)" >&2
  exit 1
fi

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

if [[ ! -d "$PREFIX/.git" ]]; then
  [[ -n "$REPO_URL" ]] || { echo "Set REPO_URL for a fresh install" >&2; exit 1; }
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$PREFIX")"
  git clone --branch "$BRANCH" "$REPO_URL" "$PREFIX"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$PREFIX/data/archive"
python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/python" -m pip install --upgrade pip
"$PREFIX/.venv/bin/pip" install "$PREFIX"

if [[ ! -f "$ENV_FILE" ]]; then
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0640 "$PREFIX/.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE from .env.example; review secrets and database settings before starting."
fi

install -o root -g root -m 0644 deploy/systemd/serpent-api.service "$UNIT_DIR/serpent-api.service"
install -o root -g root -m 0644 deploy/systemd/serpent-worker.service "$UNIT_DIR/serpent-worker.service"
install -o root -g root -m 0644 deploy/systemd/serpent-ui.service "$UNIT_DIR/serpent-ui.service"
install -o root -g root -m 0644 deploy/systemd/serpent-retention.service "$UNIT_DIR/serpent-retention.service"
install -o root -g root -m 0644 deploy/systemd/serpent-retention.timer "$UNIT_DIR/serpent-retention.timer"

chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"
"$PREFIX/.venv/bin/alembic" -c "$PREFIX/storage/alembic.ini" upgrade head
systemctl daemon-reload
systemctl enable --now serpent-api.service serpent-worker.service serpent-ui.service serpent-retention.timer
systemctl restart serpent-api.service serpent-worker.service serpent-ui.service

echo "Serpent Circle installed at $PREFIX"
