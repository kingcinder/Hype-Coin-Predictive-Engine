#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Serpent Circle Hype-Coin Predictive Engine — Local (non-root) Installer
#
# Wires a repo checkout for desktop use WITHOUT systemd/root:
#   1. Creates the project virtualenv and installs dependencies
#   2. Bootstraps the SQLite database + archive (skipped if already present)
#   3. Installs the "Serpent Circle Engine" desktop shortcut that starts the
#      whole stack (worker + API + GUI) and opens the dashboard
#
# Usage:
#   bash packaging/install-local.sh          # wire this checkout
#   bash packaging/install-local.sh --force  # re-bootstrap DB even if present
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FORCE_BOOTSTRAP=false
[[ "${1:-}" == "--force" ]] && FORCE_BOOTSTRAP=true

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "  🐍 Serpent Circle — Local Installer (no root needed)"
echo "═══════════════════════════════════════════════════════════════════"
echo ""
echo "  Project : $PROJECT_DIR"

# ── Step 1: venv + dependencies ─────────────────────────────────────────────
echo ""
echo "── Step 1/3: Python environment ────────────────────────────────"
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "  Creating virtual environment..."
    python3 -m venv "$PROJECT_DIR/.venv"
else
    echo "  ℹ️  Virtual environment already exists"
fi
echo "  Installing/updating dependencies (this may take a few minutes)..."
"$PROJECT_DIR/.venv/bin/pip" install --upgrade pip -q 2>/dev/null || true
"$PROJECT_DIR/.venv/bin/pip" install -e "${PROJECT_DIR}[dev]" -q
echo "  ✅ Dependencies installed"

# ── Step 2: database bootstrap ──────────────────────────────────────────────
echo ""
echo "── Step 2/3: Database bootstrap ────────────────────────────────"
if [[ "$FORCE_BOOTSTRAP" == "true" ]] || [[ ! -f "$PROJECT_DIR/serpent.db" ]]; then
    cd "$PROJECT_DIR"
    "$PROJECT_DIR/.venv/bin/python" scripts/bootstrap_local.py
    echo "  ✅ Database bootstrapped"
else
    echo "  ℹ️  Database already present — skipping (engine migrates on boot)"
fi

# ── Step 3: desktop shortcut ────────────────────────────────────────────────
echo ""
echo "── Step 3/3: Desktop shortcut ─────────────────────────────────"
chmod +x "$PROJECT_DIR/start-engine.sh"
LAUNCHER="$PROJECT_DIR/start-engine.sh"

mkdir -p "$HOME/.local/share/applications"
if [[ -d "$HOME/Desktop" ]]; then
    DESKTOP_COPY="$HOME/Desktop/hype-coin-engine.desktop"
else
    DESKTOP_COPY=""
fi

cat > "$HOME/.local/share/applications/serpent-engine.desktop" <<DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=Serpent Circle Engine
Comment=Start the Hype-Coin Predictive Engine (API + GUI + Worker) and open the dashboard
Exec=bash $LAUNCHER
Icon=utilities-terminal
Terminal=true
Keywords=crypto;prediction;hype;coin;serpent;
Categories=Development;Science;Finance;
StartupNotify=false
DESKTOP
echo "  ✅ Installed: ~/.local/share/applications/serpent-engine.desktop"

if [[ -n "$DESKTOP_COPY" ]]; then
    cp "$HOME/.local/share/applications/serpent-engine.desktop" "$DESKTOP_COPY"
    # GNOME requires the file to be explicitly trusted before it will launch.
    command -v gio >/dev/null 2>&1 && \
        gio set "$DESKTOP_COPY" metadata::trusted true 2>/dev/null || true
    echo "  ✅ Installed: $DESKTOP_COPY"
fi

update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "  ✅ Local install complete!"
echo ""
echo "  Double-click 'Serpent Circle Engine' on your desktop, or run:"
echo "    $LAUNCHER"
echo ""
echo "  The launcher starts the engine (worker + API + GUI) in a terminal"
echo "  and auto-opens the dashboard at http://localhost:8501 once healthy."
echo "═══════════════════════════════════════════════════════════════════"
