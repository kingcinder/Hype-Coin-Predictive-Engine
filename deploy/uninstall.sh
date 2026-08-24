#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-/opt/serpent}"
REMOVE_DATA="${REMOVE_DATA:-false}"
UNIT_DIR="/etc/systemd/system"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (for example: sudo $0)" >&2
  exit 1
fi

systemctl disable --now serpent-retention.timer serpent-api.service serpent-worker.service serpent-ui.service 2>/dev/null || true
rm -f "$UNIT_DIR/serpent-api.service" "$UNIT_DIR/serpent-worker.service" \
  "$UNIT_DIR/serpent-ui.service" "$UNIT_DIR/serpent-retention.service" \
  "$UNIT_DIR/serpent-retention.timer"
systemctl daemon-reload

if [[ "$REMOVE_DATA" == "true" ]]; then
  rm -rf -- "$PREFIX"
  echo "Removed services and application data at $PREFIX"
else
  echo "Removed services; retained $PREFIX and its database/archive."
  echo "To remove data too: REMOVE_DATA=true $0"
fi
