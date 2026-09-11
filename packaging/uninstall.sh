#!/usr/bin/env bash
# Hype-Coin Predictive Engine — clean uninstaller.
#
# Stops and removes the systemd services, deletes the install tree
# (/opt/serpent), removes the env file, drops the service user, and cleans
# up the app-grid launchers and icons. Safe to re-run: every step is
# idempotent and nothing outside the app's own footprint is touched.
set -euo pipefail

PREFIX="${PREFIX:-/opt/serpent}"
SERVICE_USER="${SERVICE_USER:-serpent}"
# Matches install.sh's ENV_FILE default; override in the environment if yours differs.
ENV_FILE="${ENV_FILE:-/etc/serpent.env}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This uninstaller needs root. Re-running with sudo…" >&2
  exec sudo -E "$0" "$@"
fi

echo "── Uninstalling Hype-Coin Predictive Engine ──"

# 1. Stop + disable services, remove unit files.
for svc in serpent-api serpent-worker serpent-ui; do
  if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${svc}\.service"; then
    echo "Stopping ${svc}…"
    systemctl stop "${svc}.service" 2>/dev/null || true
    systemctl disable "${svc}.service" 2>/dev/null || true
  fi
  rm -f "/etc/systemd/system/${svc}.service" "/lib/systemd/system/${svc}.service"
done
systemctl daemon-reload 2>/dev/null || true
echo "  ✅ Services removed"

# 2. Remove the install tree.
if [[ -e "$PREFIX" ]]; then
  echo "Removing ${PREFIX}…"
  rm -rf "$PREFIX"
  echo "  ✅ Install tree removed"
else
  echo "  ℹ️  ${PREFIX} already gone"
fi

# 3. Remove the env file (only the standard location).
if [[ -f "$ENV_FILE" ]]; then
  echo "Removing ${ENV_FILE}…"
  rm -f "$ENV_FILE"
fi

# 4. Drop the service user, but never while it still owns processes.
if id "$SERVICE_USER" >/dev/null 2>&1; then
  if pgrep -u "$SERVICE_USER" >/dev/null 2>&1; then
    echo "  ⚠️  ${SERVICE_USER} still has running processes; leaving the user in place." >&2
  else
    echo "Removing user ${SERVICE_USER}…"
    userdel -r "$SERVICE_USER" 2>/dev/null || userdel "$SERVICE_USER" 2>/dev/null || true
    echo "  ✅ User removed"
  fi
else
  echo "  ℹ️  User ${SERVICE_USER} already gone"
fi

# 5. Clean up app-grid launchers + icons for every local user that has them.
clean_user_assets() {
  local home="$1"
  rm -f "$home/.local/share/applications/hype-coin-uninstall.desktop"
  rm -f "$home/.local/share/applications/hype-coin-install.desktop"
  rm -f "$home/.local/share/icons/hicolor/256x256/apps/hype-coin-install.png"
  rm -f "$home/.local/share/icons/hicolor/256x256/apps/hype-coin-uninstall.png"
}
for home in /home/* /root; do
  [ -d "$home" ] || continue
  clean_user_assets "$home"
done
if [[ -n "${SUDO_USER:-}" ]]; then
  uhome="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
  [ -n "$uhome" ] && clean_user_assets "$uhome"
fi
echo "  ✅ App-grid entries removed"

echo ""
echo "✅ Hype-Coin Predictive Engine uninstalled."
