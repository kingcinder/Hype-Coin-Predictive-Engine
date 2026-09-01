#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Serpent Circle Hype-Coin Predictive Engine — One-Click Installer
#
# Usage:
#   sudo bash install.sh              # install to /opt/serpent
#   sudo bash install.sh --prefix /custom/path   # custom location
#
# After install, manage with:
#   serpent start | stop | restart | status | logs | update | uninstall
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
PREFIX="${PREFIX:-/opt/serpent}"
SERVICE_USER="${SERVICE_USER:-serpent}"
BRANCH="${BRANCH:-main}"
REPO_URL="${REPO_URL:-https://github.com/kingcinder/Hype-Coin-Predictive-Engine.git}"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Argument parsing (while-loop with proper shift) ─────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)   shift; PREFIX="${1:-/opt/serpent}" ;;
    --prefix=*) PREFIX="${1#*=}" ;;
    --branch)   shift; BRANCH="${1:-main}" ;;
    --branch=*) BRANCH="${1#*=}" ;;
    --user)     shift; SERVICE_USER="${1:-serpent}" ;;
    --user=*)   SERVICE_USER="${1#*=}" ;;
    --help|-h)
      echo "Usage: sudo bash install.sh [--prefix /path] [--branch name] [--user username]"
      echo "  --prefix   Installation directory (default: /opt/serpent)"
      echo "  --branch   Git branch to track (default: main)"
      echo "  --user     System user for the service (default: serpent)"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

# ── Preflight checks ──────────────────────────────────────────────────────
if [[ "${EUID}" -ne 0 ]]; then
  echo "❌ This installer must be run as root." >&2
  echo "   Use: sudo bash $0" >&2
  exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "  🐍 Serpent Circle — Hype-Coin Predictive Engine Installer"
echo "═══════════════════════════════════════════════════════════════════"
echo ""
echo "  Install dir : $PREFIX"
echo "  Service user: $SERVICE_USER"
echo "  Repo URL    : $REPO_URL"
echo "  Branch      : $BRANCH"
echo ""

# Check for required tools
command -v git >/dev/null 2>&1  || { echo "❌ git is required. Install with: sudo apt install git" >&2; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo "❌ systemd is required." >&2; exit 1; }

# Find Python 3.10+
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
    major=$(echo "$version" | cut -d. -f1)
    minor=$(echo "$version" | cut -d. -f2)
    if [[ "$major" -ge 3 ]] && [[ "$minor" -ge 10 ]]; then
      PYTHON="$(command -v "$candidate")"
      echo "  Found Python: $PYTHON ($version)"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  echo "❌ Python 3.10+ is required but not found." >&2
  echo "   Install with: sudo apt install python3.12 python3.12-venv python3.12-dev" >&2
  exit 1
fi

echo ""

# ── Step 1: Create service user ────────────────────────────────────────────
echo "── Step 1/7: Creating service user ──────────────────────────────"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  echo "  ✅ Created user: $SERVICE_USER"
else
  echo "  ℹ️  User $SERVICE_USER already exists"
fi

# ── Step 2: Clone or update repo ───────────────────────────────────────────
echo "── Step 2/7: Setting up source code ────────────────────────────"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$PREFIX")"

if [[ ! -d "$PREFIX/.git" ]]; then
  echo "  Cloning repository..."
  git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$PREFIX"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"
  echo "  ✅ Cloned to $PREFIX"
else
  echo "  ℹ️  Repository already exists at $PREFIX, pulling latest..."
  cd "$PREFIX"
  git fetch origin "$BRANCH" 2>/dev/null || true
  git checkout "$BRANCH" 2>/dev/null || true
  git pull --ff-only origin "$BRANCH" 2>/dev/null || true
  echo "  ✅ Updated to latest"
fi

# ── Step 3: Create venv and install dependencies ───────────────────────────
echo "── Step 3/7: Installing Python environment ──────────────────────"
if [[ ! -d "$PREFIX/.venv" ]] || [[ ! -f "$PREFIX/.venv/bin/activate" ]]; then
  echo "  Creating virtual environment..."
  "$PYTHON" -m venv "$PREFIX/.venv"
  echo "  ✅ Created venv"
fi

# Activate and install
source "$PREFIX/.venv/bin/activate"
echo "  Installing/updating dependencies (this may take a few minutes)..."
"$PREFIX/.venv/bin/pip" install --upgrade pip -q 2>/dev/null
"$PREFIX/.venv/bin/pip" install -e "$PREFIX" -q 2>/dev/null
echo "  ✅ Dependencies installed"

# ── Step 4: Create .env if missing ─────────────────────────────────────────
echo "── Step 4/7: Configuring environment ───────────────────────────"
ENV_FILE="$PREFIX/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'ENVEOF'
# Serpent Circle — Environment Configuration
# See .env.example for all available options.

# Zero-container profile: SQLite + local Parquet archive
env=local-single

# Database (SQLite by default when env=local-single)
# database_url=sqlite:///serpent.db

# API / GUI ports
api_port=8000
ui_port=8501

# Scan cadence (seconds between ingestion iterations)
scan_interval_seconds=300

# Night crawlers (expanded data sources)
nightcrawler_enabled=true
nightcrawler_interval_minutes=30

# Data lake (signal scoring, label densification, webhooks)
data_lake_enabled=true
webhook_enabled=true

# Archive & retention
archive_enabled=true
archive_backend=local
archive_local_dir=data/archive
archive_retention_days=90

# Forecast model
forecast_enabled=true
forecast_train_frequency_hours=24

# Risk calibration
risk_outcome_window_hours=48
risk_calibration_frequency_hours=24

# LLM integration (set llm_enabled=false if Ollama not installed)
llm_enabled=false
llm_model=qwen2.5:0.5b

# Push notifications (ntfy.sh — free, no account needed)
# ntfy_enabled=true
# ntfy_topic=my-serpent-alerts
ENVEOF
  chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  echo "  ✅ Created $ENV_FILE (review and customize as needed)"
else
  echo "  ℹ️  $ENV_FILE already exists, skipping"
fi

# ── Step 5: Run database migrations ────────────────────────────────────────
echo "── Step 5/7: Running database migrations ────────────────────────"
# Let alembic handle everything: on a fresh DB, upgrade head creates all
# tables through the migration chain. On an existing DB, it applies pending
# migrations only. This avoids the create_all + alembic version mismatch.
if [[ -f "$PREFIX/storage/alembic.ini" ]]; then
  cd "$PREFIX"
  "$PREFIX/.venv/bin/alembic" -c storage/alembic.ini upgrade head 2>/dev/null || \
    echo "  ⚠️  Alembic migrations completed with warnings"
else
  # Fallback: no alembic.ini, use create_all directly
  "$PREFIX/.venv/bin/python" -c "
from storage.database import Base, engine
from storage import models  # noqa: registers metadata
Base.metadata.create_all(bind=engine)
print('  ✅ Database schema created/verified')
" 2>/dev/null || echo "  ⚠️  Schema creation skipped"
fi

# Ensure archive directory exists
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$PREFIX/data/archive"

# ── Step 6: Install systemd unit ───────────────────────────────────────────
echo "── Step 6/7: Installing system services ─────────────────────────"

cat > "$UNIT_DIR/serpent.service" <<UNIT
[Unit]
Description=Serpent Circle Hype-Coin Predictive Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PREFIX
EnvironmentFile=-$PREFIX/.env
Environment=PYTHONUNBUFFERED=1
User=$SERVICE_USER
Group=$SERVICE_USER
ExecStart=$PREFIX/.venv/bin/python -m engine
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable serpent.service 2>/dev/null || true
echo "  ✅ systemd service installed and enabled"

# ── Step 7: Install CLI shim + metadata ────────────────────────────────────
echo "── Step 7/7: Installing CLI command ────────────────────────────"

# Store the branch for updates
echo "$BRANCH" > "$PREFIX/.serpent-branch"

# Install the CLI shim from the repo if it exists, otherwise use embedded
if [[ -f "$PREFIX/packaging/serpent" ]]; then
  cp "$PREFIX/packaging/serpent" /usr/local/bin/serpent
  chmod +x /usr/local/bin/serpent
else
  cat > /usr/local/bin/serpent <<'CLIEOF'
#!/usr/bin/env bash
set -euo pipefail

PREFIX="${SERPENT_PREFIX:-/opt/serpent}"
SERVICE="serpent.service"

usage() {
  cat <<EOF
Serpent Circle — Hype-Coin Predictive Engine

Usage: serpent <command>

Commands:
  start       Start the engine (API + GUI + Worker + Crawlers)
  stop        Stop the engine
  restart     Restart the engine
  status      Show service status
  logs        Follow live logs (Ctrl+C to stop)
  logs-recent Show last 50 lines of logs
  update      Pull latest code, run migrations, restart
  uninstall   Remove services and CLI (keeps data by default)
  version     Show installed version
  open        Open the GUI in your default browser

Options:
  --remove-data   Use with 'uninstall' to also delete the database
                  and all collected data (irreversible!)

Examples:
  serpent start
  serpent status
  serpent logs
  serpent update
  serpent uninstall --remove-data
EOF
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "❌ This command requires root. Use: sudo serpent $1" >&2
    exit 1
  fi
}

REMOVE_DATA=false
for _arg in "$@"; do
  [[ "$_arg" == "--remove-data" ]] && REMOVE_DATA=true
done

case "${1:-help}" in
  start)
    require_root start
    systemctl start "$SERVICE"
    echo "✅ Serpent Circle engine started"
    echo "   GUI: http://localhost:8501"
    echo "   API: http://localhost:8000/health"
    ;;
  stop)
    require_root stop
    systemctl stop "$SERVICE"
    echo "✅ Serpent Circle engine stopped"
    ;;
  restart)
    require_root restart
    systemctl restart "$SERVICE"
    echo "✅ Serpent Circle engine restarted"
    ;;
  status)
    systemctl --no-pager status "$SERVICE" 2>/dev/null || true
    echo ""
    echo "── Endpoints ──"
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
      echo "  GUI : http://localhost:8501"
      echo "  API : http://localhost:8000/health"
      if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
        echo "  Health: ✅ responding"
      else
        echo "  Health: ⚠️  not responding yet (may still be starting)"
      fi
    else
      echo "  Service is not running"
    fi
    ;;
  logs)
    journalctl -u "$SERVICE" -f --no-pager
    ;;
  logs-recent)
    journalctl -u "$SERVICE" -n 50 --no-pager
    ;;
  update)
    require_root update
    if [[ ! -d "$PREFIX/.git" ]]; then
      echo "❌ Not an installed instance at $PREFIX" >&2
      exit 1
    fi
    BRANCH=$(cat "$PREFIX/.serpent-branch" 2>/dev/null || echo "main")
    echo "── Pulling latest from branch: $BRANCH"
    cd "$PREFIX"
    git fetch origin "$BRANCH"
    git checkout "$BRANCH"
    git pull --ff-only origin "$BRANCH"
    echo "── Updating dependencies..."
    "$PREFIX/.venv/bin/pip" install -e "$PREFIX" -q 2>/dev/null
    echo "── Running migrations..."
    "$PREFIX/.venv/bin/alembic" -c storage/alembic.ini upgrade head 2>/dev/null || \
      "$PREFIX/.venv/bin/python" -c "
from storage.database import Base, engine
from storage import models
Base.metadata.create_all(bind=engine)
" 2>/dev/null || true
    echo "── Restarting service..."
    systemctl daemon-reload
    systemctl restart "$SERVICE"
    echo "✅ Updated and restarted"
    ;;
  uninstall)
    require_root uninstall
    if [[ "$REMOVE_DATA" == "true" ]]; then
      echo ""
      echo "⚠️  WARNING: This will permanently delete:"
      echo "   - All databases and Parquet archives at $PREFIX"
      echo "   - All configuration files"
      echo "   - The entire installation directory"
      echo ""
      read -r -p "Are you sure? Type 'yes' to confirm: " confirm
      if [[ "$confirm" != "yes" ]]; then
        echo "Aborted."
        exit 0
      fi
    fi
    echo "── Stopping and disabling service..."
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE"
    systemctl daemon-reload
    rm -f /usr/local/bin/serpent
    if [[ "$REMOVE_DATA" == "true" ]]; then
      echo "── Removing all data..."
      rm -rf -- "$PREFIX"
      echo "✅ Removed services, CLI, and all data at $PREFIX"
    else
      echo "✅ Removed service and CLI"
      echo "   Data retained at $PREFIX (database + archive)"
      echo "   To remove data too: sudo serpent uninstall --remove-data"
    fi
    ;;
  version)
    if [[ -f "$PREFIX/pyproject.toml" ]]; then
      grep -oP 'version = "\K[^"]+' "$PREFIX/pyproject.toml" 2>/dev/null || echo "unknown"
    else
      echo "not installed"
    fi
    ;;
  open)
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
      xdg-open http://localhost:8501 2>/dev/null || \
      sensible-browser http://localhost:8501 2>/dev/null || \
      echo "Open http://localhost:8501 in your browser"
    else
      echo "⚠️  Engine is not running. Start it first: sudo serpent start"
      echo "   Then open: http://localhost:8501"
    fi
    ;;
  help|--help|-h)
    usage
    ;;
  *)
    echo "❌ Unknown command: $1" >&2
    echo "   Run 'serpent help' for usage" >&2
    exit 1
    ;;
esac
CLIEOF
  chmod +x /usr/local/bin/serpent
fi
echo "  ✅ CLI command installed: /usr/local/bin/serpent"

# ── Create desktop entry ───────────────────────────────────────────────────
DESKTOP_DIR="/usr/share/applications"
if [[ -d "$DESKTOP_DIR" ]]; then
  # `serpent start` needs root to talk to systemd, so the entry elevates via
  # pkexec (PolicyKit password prompt), waits for the API to answer, then
  # opens the GUI. Plain `Exec=... serpent start` would fail silently for a
  # non-root user, which is why it must not be called directly.
  cat > "$DESKTOP_DIR/serpent-engine.desktop" <<DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=Serpent Circle Engine
Comment=Hype-Coin Predictive Engine — start engine and open the dashboard
Exec=bash -c 'pkexec /usr/local/bin/serpent start; for i in $(seq 1 60); do curl -sf http://localhost:8000/health >/dev/null 2>&1 && break; sleep 1; done; xdg-open http://localhost:8501'
Icon=utilities-terminal
Terminal=false
Categories=Finance;Science;Development;
Keywords=crypto;prediction;hype;coin;serpent;blockchain;
StartupNotify=false
DESKTOP
  echo "  ✅ Desktop entry installed (starts service via pkexec, opens GUI)"
fi

# ── Fix ownership ──────────────────────────────────────────────────────────
chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX" 2>/dev/null || true

# ── Final message ──────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "  ✅ Installation complete!"
echo "═══════════════════════════════════════════════════════════════════"
echo ""
echo "  Quick start:"
echo "    sudo serpent start"
echo ""
echo "  Then open:"
echo "    http://localhost:8501"
echo ""
echo "  Manage the engine:"
echo "    serpent status     — check if it's running"
echo "    serpent logs       — watch live logs"
echo "    serpent stop       — shut down"
echo "    serpent restart    — restart"
echo "    serpent update     — pull latest + restart"
echo "    serpent uninstall  — remove (keeps data)"
echo ""
echo "  Edit config:"
echo "    sudo nano $PREFIX/.env"
echo ""
echo "═══════════════════════════════════════════════════════════════════"
