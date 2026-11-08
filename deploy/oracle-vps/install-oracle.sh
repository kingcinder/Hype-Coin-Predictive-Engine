#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Serpent Circle — Zero-Config Oracle VPS Installer (francisco-build)
#
# Goal: as little configuration as possible between a fresh Ubuntu VPS
# (e.g. Oracle Cloud Always Free, ARM or x86) and a running engine whose
# alerts reach the owner's phone.
#
# What this script does, start to finish:
#   1. Installs the OS prerequisites (git, Python 3.12, venv, build tools)
#      if they are missing.
#   2. Runs the shipped system installer (packaging/install.sh) tracking
#      the francisco-build branch — service user, venv, SQLite database,
#      systemd unit, `serpent` CLI.
#   3. Fills in the ONLY settings a phone-first deployment needs, in
#      /opt/serpent/.env, without asking questions:
#        - ENGINE_API_TOKEN: generated if unset (the engine also
#          self-generates one via ensure_api_token; this makes it
#          deterministic and present before first start). The token is
#          never printed — it stays in the root/serpent-only .env.
#        - ntfy push notifications ON, with a private topic (yours if you
#          pass NTFY_TOPIC, otherwise a generated unguessable one that
#          this script prints ONCE at the end — subscribe to it in the
#          free ntfy app on the phone).
#        - ntfy_alert_types_csv set to ALL engine alert event types.
#        - llm_enabled=false (a free-tier VPS has no room for a local
#          model; the engine's core scoring does not need it).
#   4. If HOOK_URL is set in the environment, registers a webhook for
#      that URL subscribed to EVERY webhook event type
#      (deploy/oracle-vps/register-hooks.py).
#   5. Restarts the service and runs the authenticated health check the
#      same way the shipped launcher does (token via curl stdin config,
#      never in the process list).
#
# Usage (on the VPS, as a sudo-capable user):
#   git clone -b francisco-build \
#     https://github.com/kingcinder/Hype-Coin-Predictive-Engine.git
#   cd Hype-Coin-Predictive-Engine
#   sudo bash deploy/oracle-vps/install-oracle.sh
#
# Optional environment:
#   NTFY_TOPIC   use this ntfy topic instead of generating one
#   HOOK_URL     also register this webhook URL for all event types
#   BRANCH       branch for the system installer to track (default:
#                francisco-build)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BRANCH="${BRANCH:-francisco-build}"
PREFIX="${PREFIX:-/opt/serpent}"
ENV_FILE="$PREFIX/.env"
REPO_URL="${REPO_URL:-https://github.com/kingcinder/Hype-Coin-Predictive-Engine.git}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "❌ Run with sudo: sudo bash $0" >&2
  exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  🐍 Serpent Circle — Zero-Config Oracle VPS Install"
echo "═══════════════════════════════════════════════════════════════"

# ── Step 1: OS prerequisites ────────────────────────────────────────────────
echo "── Step 1/5: OS prerequisites ───────────────────────────────────"
missing=()
command -v git >/dev/null 2>&1 || missing+=(git)
command -v curl >/dev/null 2>&1 || missing+=(curl)
python3.12 --version >/dev/null 2>&1 || missing+=(python3.12 python3.12-venv python3.12-dev)
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "  Installing: ${missing[*]}"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" build-essential
else
  echo "  ✅ git, curl, Python 3.12 present"
fi

# ── Step 2: Shipped system installer ────────────────────────────────────────
echo "── Step 2/5: System installer (branch: $BRANCH) ─────────────────"
BRANCH="$BRANCH" REPO_URL="$REPO_URL" bash "$REPO_DIR/packaging/install.sh"

# ── Step 3: Zero-config .env fill-ins (idempotent) ──────────────────────────
echo "── Step 3/5: Zero-config settings ───────────────────────────────"
set_env() { # set_env KEY VALUE — replace or append, preserving the file
  local key="$1" value="$2"
  if grep -qE "^[#[:space:]]*${key}=" "$ENV_FILE"; then
    sed -i "s|^[#[:space:]]*${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

# API token: generate only if absent/empty, so re-runs never rotate it.
if ! grep -qE "^ENGINE_API_TOKEN=." "$ENV_FILE"; then
  set_env ENGINE_API_TOKEN "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "  ✅ ENGINE_API_TOKEN generated (stored in $ENV_FILE, not printed)"
fi

# ntfy push: enable, all alert types, topic = provided or generated once.
if [[ -z "${NTFY_TOPIC:-}" ]]; then
  existing="$(sed -n 's/^ntfy_topic=//p' "$ENV_FILE" | tail -n 1 | tr -d '\r')"
  if [[ -n "$existing" ]]; then
    NTFY_TOPIC="$existing"
  else
    NTFY_TOPIC="serpent-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
fi
set_env ntfy_enabled true
set_env ntfy_topic "$NTFY_TOPIC"
set_env ntfy_alert_types_csv "ignition_detected,liquidity_withdrawal_warning,syndicate_recidivism,lifecycle_transition,high_signal_scan"
# Free-tier VPS: no local LLM.
set_env llm_enabled false
chown serpent:serpent "$ENV_FILE" 2>/dev/null || true
chmod 640 "$ENV_FILE"
echo "  ✅ ntfy enabled for ALL alert types; LLM layer off"

# ── Step 4: Optional webhook for every event type ───────────────────────────
echo "── Step 4/5: Webhook hooks ──────────────────────────────────────"
if [[ -n "${HOOK_URL:-}" ]]; then
  sudo -u serpent "$PREFIX/.venv/bin/python" \
    "$REPO_DIR/deploy/oracle-vps/register-hooks.py" "$HOOK_URL"
else
  echo "  ℹ️  HOOK_URL not set — ntfy push (step 3) is the phone alert path."
  echo "     Re-run with HOOK_URL=<url> to also register a webhook for"
  echo "     every event type (Telegram/Discord/custom endpoint)."
fi

# ── Step 5: Restart + authenticated health check ────────────────────────────
echo "── Step 5/5: Restart and verify ─────────────────────────────────"
systemctl restart serpent.service
for i in $(seq 1 30); do
  tok="$(sed -n 's/^ENGINE_API_TOKEN=//p' "$ENV_FILE" | tail -n 1 | tr -d "\"'" | tr -d '[:space:]')"
  if printf 'header = "Authorization: Bearer %s"\n' "$tok" \
       | curl -sf -K - "http://localhost:8000/health" >/dev/null 2>&1; then
    echo "  ✅ Engine healthy (authenticated /health OK)"
    break
  fi
  [[ "$i" == "30" ]] && { echo "  ❌ Health check failed — see: sudo serpent logs"; exit 1; }
  sleep 2
done

cat <<EOF

═══════════════════════════════════════════════════════════════
  ✅ DONE — the engine is running and scanning.

  📱 On the phone:
     1. Install the free "ntfy" app (Android/iOS).
     2. Subscribe to this topic — treat it like a password, it is
        the only thing between the public and your alerts:
            $NTFY_TOPIC
        (server: https://ntfy.sh — already configured on the VPS)
     3. Alerts for every event type now push to the phone.

  📊 Dashboard (phone-friendly GUI) — from your computer:
        ssh -L 8501:localhost:8501 <user>@<this-vps>
     then open http://localhost:8501 in the phone/computer browser
     over that tunnel. (Or browse to the VPS on a private mesh VPN.)

  🔧 Manage:  sudo serpent status | logs | restart | update
═══════════════════════════════════════════════════════════════
EOF
