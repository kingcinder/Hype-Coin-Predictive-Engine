#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Serpent Circle Engine — One-Click Launcher
# Double-click the desktop shortcut or run this script directly.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# Resolve the project directory relative to this script so the launcher
# works from any checkout location (desktop shortcut, CLI, CI). Override
# with SERPENT_PROJECT_DIR if you keep the script elsewhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SERPENT_PROJECT_DIR:-$SCRIPT_DIR}"

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

# Spawn a helper that surfaces the GUI as soon as the API is healthy,
# then hands back to the engine (exec keeps Ctrl+C wired to the engine).
(
    API_PORT="${SERPENT_API_PORT:-8000}"
    UI_PORT="${SERPENT_UI_PORT:-8501}"
    for _ in $(seq 1 90); do
        if curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
            sleep 2  # let the Streamlit GUI settle before opening the browser
            xdg-open "http://localhost:${UI_PORT}" >/dev/null 2>&1 \
                || sensible-browser "http://localhost:${UI_PORT}" >/dev/null 2>&1 \
                || true
            exit 0
        fi
        sleep 1
    done
    echo "⚠️  Engine did not become healthy in 90s — open http://localhost:${UI_PORT} manually."
) &

# exec replaces the shell so Ctrl+C goes straight to the engine
exec python -m engine
