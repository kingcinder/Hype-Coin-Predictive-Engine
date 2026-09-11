#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Serpent Circle — split-services systemd installer.
#
# Installs serpent-api.service, serpent-worker.service, serpent-ui.service and
# the serpent-retention timer into /opt/serpent (or $PREFIX).
#
# NOTE: this is the *split-services* installer. It does NOT install the
# `serpent` CLI (that ships with packaging/install.sh, the all-in-one
# installer documented in INSTALL.md, which manages a single
# serpent.service). Manage the units installed here directly with systemctl,
# e.g. `systemctl status serpent-api serpent-worker serpent-ui`.
# ─────────────────────────────────────────────────────────────────────────────
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
  # Create the parent directory without changing its ownership: taking over
  # /opt (or any other parent) for the service user is overbroad (M20).
  install -d "$(dirname "$PREFIX")"
  if [[ -e "$PREFIX" ]]; then
    # A previous install attempt left a non-repo directory behind (e.g. a
    # failed clone). Preserve it instead of aborting: move it aside with a
    # timestamp so no existing data is ever silently destroyed.
    backup="${PREFIX}.bak-$(date +%Y%m%d-%H%M%S)"
    echo "WARNING: $PREFIX exists but is not a git checkout -- moving it to $backup" >&2
    mv "$PREFIX" "$backup"
  fi
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
# Record the tracked branch: the `serpent update` path (packaging/install.sh)
# reads this file, and keeping it here keeps both installers consistent.
echo "$BRANCH" > "$PREFIX/.serpent-branch"
chown "$SERVICE_USER:$SERVICE_USER" "$PREFIX/.serpent-branch"
# Run migrations from $PREFIX: common/config.py resolves env_file=".env"
# relative to the process cwd, so running alembic from anywhere else can
# migrate the wrong database (M19).
cd "$PREFIX"
"$PREFIX/.venv/bin/alembic" -c "$PREFIX/storage/alembic.ini" upgrade head
systemctl daemon-reload
systemctl enable --now serpent-api.service serpent-worker.service serpent-ui.service serpent-retention.timer
systemctl restart serpent-api.service serpent-worker.service serpent-ui.service

echo "Serpent Circle installed at $PREFIX"
echo "Split-services profile: manage units with systemctl, e.g."
echo "  systemctl status serpent-api serpent-worker serpent-ui"
echo "(The 'serpent' CLI is only installed by packaging/install.sh.)"
