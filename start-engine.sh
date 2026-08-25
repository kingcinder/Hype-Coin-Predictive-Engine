#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Serpent Circle Engine — One-Click Launcher
# Double-click the desktop shortcut or run this script directly.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_DIR="/home/cody/Documents/Hype-Coin-Predictive-Engine-main"

# ── Step 1: Navigate to project ──────────────────────────────────────
cd "$PROJECT_DIR" || {
    echo "❌ Project directory not found: $PROJECT_DIR"
    exit 1
}
echo "📂 Project: $PROJECT_DIR"

# ── Step 2: Ensure venv exists ───────────────────────────────────────
if [ ! -d ".venv" ] || [ ! -f ".venv/bin/activate" ]; then
    echo "🔧 Creating virtual environment..."
    python3 -m venv .venv
    echo "📦 Installing dependencies..."
    .venv/bin/pip install -e ".[dev]"
fi

# ── Step 3: Activate venv ────────────────────────────────────────────
source .venv/bin/activate
echo "🐍 Python: $(which python) ($(python --version 2>&1))"

# ── Step 4: Launch the engine ────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  🐍 Serpent Circle Hype-Coin Predictive Engine"
echo "═══════════════════════════════════════════════════════════"
echo "  API : http://localhost:8000"
echo "  GUI : http://localhost:8501"
echo "  Ctrl+C to stop"
echo "═══════════════════════════════════════════════════════════"
echo ""

# exec replaces the shell so Ctrl+C goes straight to the engine
exec python -m engine
