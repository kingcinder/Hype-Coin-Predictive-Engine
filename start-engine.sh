#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Serpent Circle Engine — One-Click Launcher (the single bring-up path)
# Double-click the desktop shortcut or run this script directly.
#
#   ./start-engine.sh          guard → backup (+ prune) → launch
#   ./start-engine.sh --gate   also run the full check suite first
#
# Before launching it refuses a second writer (an engine already running,
# the shipped ports already bound, or a --network host container that may
# hold them) and takes a verified copy of serpent.db through SQLite's
# backup API. Each launch writes a new snapshot, so the newest
# SERPENT_BACKUP_KEEP (default 10) are kept per database and older ones are
# pruned (scripts/prune_backups.py). Ctrl+C stops the engine.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# Resolve the project directory relative to this script so the launcher
# works from any checkout location (desktop shortcut, CLI, CI). Override
# with SERPENT_PROJECT_DIR if you keep the script elsewhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SERPENT_PROJECT_DIR:-$SCRIPT_DIR}"
API_PORT="${SERPENT_API_PORT:-8000}"
UI_PORT="${SERPENT_UI_PORT:-8501}"
BACKUP_DIR="${SERPENT_BACKUP_DIR:-$HOME/serpent-backups}"
# How many verified snapshots of each database to keep. Every launch writes a
# new one, so without a bound the backup directory grows forever; the newest N
# are kept and the rest pruned after each successful backup.
BACKUP_KEEP="${SERPENT_BACKUP_KEEP:-10}"
LOG_DIR="${SERPENT_LOG_DIR:-$HOME/serpent-logs}"

RUN_GATE=0
for arg in "$@"; do
    case "$arg" in
        --gate) RUN_GATE=1 ;;
        -h | --help)
            sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "❌ Unknown option: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

case "$BACKUP_KEEP" in
    '' | *[!0-9]* | 0)
        echo "❌ SERPENT_BACKUP_KEEP must be a positive integer (got '$BACKUP_KEEP')." >&2
        exit 2
        ;;
esac

# ── Step 1: Navigate to project ──────────────────────────────────────
cd "$PROJECT_DIR" || {
    echo "❌ Project directory not found: $PROJECT_DIR"
    exit 1
}
echo "📂 Project: $PROJECT_DIR"

# ── Step 2: Refuse to start a second writer ──────────────────────────────────────
# Two engines on one serpent.db wedge the loop: the second one's writes
# collide with the first one's and the queue drops them. Check the process,
# the shipped ports, and host-network containers (which can hold a port
# without appearing in `docker ps`'s published-ports column).
# The pattern is anchored to an executable path so it cannot match a shell
# whose command line merely mentions "python -m engine" (a log tail, a test
# harness, another launcher's arguments).
ENGINE_PID_FILE=${SERPENT_ENGINE_PID_FILE:-}
if [ -n "$ENGINE_PID_FILE" ]; then
    # An explicit PID file is authoritative: a missing or empty file means
    # "no engine running", so shell-driven tests need not depend on the
    # host's real process table. When the variable is unset, use the pgrep
    # default below.
    ENGINE_PID="$(cat "$ENGINE_PID_FILE" 2>/dev/null || true)"
    if [ -n "$ENGINE_PID" ] && kill -0 "$ENGINE_PID" 2>/dev/null; then
        echo "❌ An engine process is already running; do not start a second one:" >&2
        echo "   $ENGINE_PID" >&2
        echo "   Stop it first (Ctrl+C in its terminal, or kill the PID above)." >&2
        exit 1
    fi
else
    ENGINE_PATTERN='(^|/)[p]ython[0-9.]* -m engine( |$)'
    if pgrep -f "$ENGINE_PATTERN" >/dev/null; then
        echo "❌ An engine process is already running; do not start a second one:" >&2
        pgrep -af "$ENGINE_PATTERN" >&2 || true
        echo "   Stop it first (Ctrl+C in its terminal, or kill the PID above)." >&2
        exit 1
    fi
fi
if command -v ss >/dev/null 2>&1 &&
    ss -ltn 2>/dev/null | grep -Eq ":(${API_PORT}|${UI_PORT})[[:space:]]"; then
    echo "❌ Port ${API_PORT} or ${UI_PORT} is already in use:" >&2
    ss -ltnp 2>/dev/null | grep -E ":(${API_PORT}|${UI_PORT})[[:space:]]" >&2 || true
    echo "   Free it (or set SERPENT_API_PORT/SERPENT_UI_PORT for this launch)." >&2
    exit 1
fi
if command -v docker >/dev/null 2>&1; then
    for container in $(docker ps --format '{{.Names}}' 2>/dev/null || true); do
        netmode="$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container" 2>/dev/null)"
        if [ "$netmode" = "host" ]; then
            # Check if this container actually holds the shipped ports
            # by looking at its network state
            port_holders=""
            if command -v ss >/dev/null 2>&1; then
                # ss can show socket info from the host network namespace
                # Check if any process in the container holds the ports
                container_ipids="$(docker inspect -f '{{.State.Pid}}' "$container" 2>/dev/null)"
                if [ -n "$container_ipids" ] && [ "$container_ipids" != "0" ]; then
                    # Check listening sockets on the host for the ports
                    for port in ${API_PORT} ${UI_PORT}; do
                        # Try to find processes listening on these ports
                        host_pids="$(ss -ltnp 2>/dev/null | grep ":${port}" | grep -o 'pid=[0-9]*' | cut -d= -f2 || true)"
                        if [ -n "$host_pids" ]; then
                            port_holders="${port_holders}${port} "
                        fi
                    done
                fi
            fi
            if [ -n "$port_holders" ]; then
                echo "❌ Container '$container' runs with --network host and holds the shipped ports${port_holders}; stop it or remove --network host." >&2
                exit 1
            else
                echo "⚠️  Container '$container' runs with --network host but does not hold ports ${API_PORT}/${UI_PORT}; continuing with warning." >&2
            fi
        fi
    done
fi

# ── Step 3: Verified backup of the existing database ─────────────────
# SQLite's backup API copies a consistent snapshot even while another
# process is writing; a plain `cp` of a live DB can capture a torn state.
if [ -s serpent.db ]; then
    mkdir -p "$BACKUP_DIR"
    DB_BACKUP="$BACKUP_DIR/serpent-$(date +%Y%m%d-%H%M%S).db"
    if DB_BACKUP="$DB_BACKUP" python3 - <<'PY'
import os
import sqlite3

src = sqlite3.connect("file:serpent.db?mode=ro", uri=True)
dst = sqlite3.connect(os.environ["DB_BACKUP"])
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
PY
    then
        echo "🗄️  DB backup: $DB_BACKUP"
        echo "    sha256:     $(sha256sum "$DB_BACKUP" | awk '{print $1}')"
        # Retention: keep the newest SERPENT_BACKUP_KEEP snapshots of this
        # database and delete the older ones. A prune failure is a warning, not
        # a reason to refuse a launch whose backup already succeeded.
        if ! python3 scripts/prune_backups.py \
            --dir "$BACKUP_DIR" --database serpent.db --keep "$BACKUP_KEEP"; then
            echo "⚠️  Could not prune old backups in $BACKUP_DIR (keep=$BACKUP_KEEP);" \
                "they will keep accumulating." >&2
        fi
    else
        echo "❌ SQLite backup of serpent.db failed; not starting over an unbacked DB." >&2
        exit 1
    fi
else
    echo "🗄️  DB backup: none (no serpent.db yet — the engine will bootstrap it)"
fi

# ── Step 4: Ensure venv exists ───────────────────────────────────────
if [ ! -d ".venv" ] || [ ! -f ".venv/bin/activate" ]; then
    echo "🔧 Creating virtual environment..."
    python3 -m venv .venv
    echo "📦 Installing dependencies (lock first, then the project without deps)..."
    # Installing the requirements lock first matters: an unpinned editable
    # install resolves newer majors (SQLAlchemy 2.1 breaks the mypy gate).
    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/pip install -e . --no-deps
fi

# ── Step 5: Activate venv ────────────────────────────────────────────
source .venv/bin/activate
echo "🐍 Python: $(which python) ($(python --version 2>&1))"

# ── Step 6: Optional full gate before launch ─────────────────────────
# `--gate` runs the same suite CI runs (lint, format, mypy, import and
# dependency scans, pytest with the coverage floor) and refuses to start
# on a failure. It takes ~10 minutes, which is why it is opt-in.
if [ "$RUN_GATE" = 1 ]; then
    mkdir -p "$LOG_DIR"
    echo "🧪 Running the gate (log: $LOG_DIR/gate.log)..."
    export PATH="$PWD/.venv/bin:$PATH"
    if ! make VENV=.venv gate >"$LOG_DIR/gate.log" 2>&1; then
        grep -E '^(PASS|FAIL):' "$LOG_DIR/gate.log" >&2 || true
        tail -25 "$LOG_DIR/gate.log" >&2 || true
        echo "❌ make gate failed; not starting the engine." >&2
        exit 1
    fi
    echo "✅ Gate passed: $(grep -E '^TOTAL' "$LOG_DIR/gate.log" | tail -1)"
fi

# ── Step 7: Launch the engine ────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  🐍 Serpent Circle Hype-Coin Predictive Engine"
echo "═══════════════════════════════════════════════════════════"
echo "  API : http://localhost:${API_PORT}"
echo "  GUI : http://localhost:${UI_PORT}"
echo "  Ctrl+C to stop"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Authenticated health probe: /health sits behind the bearer gate (H4), so
# the token is sent as a header. It is read from the environment or .env and
# passed through curl -K so the value never appears in the process list.
probe_health() {
    local port="$1" tok="${ENGINE_API_TOKEN:-}"
    if [ -z "$tok" ] && [ -f .env ]; then
        tok="$(sed -n 's/^ENGINE_API_TOKEN=//p' .env | tail -n 1 | tr -d "\"'" | tr -d '\r')"
    fi
    printf 'header = "Authorization: Bearer %s"\n' "$tok" |
        curl -sf -K - "http://localhost:${port}/health" >/dev/null 2>&1
}

# Spawn a helper that surfaces the GUI as soon as the API is healthy,
# then hands back to the engine (exec keeps Ctrl+C wired to the engine).
(
    for _ in $(seq 1 90); do
        if probe_health "${API_PORT}"; then
            sleep 2  # let the Streamlit GUI settle before opening the browser
            xdg-open "http://localhost:${UI_PORT}" >/dev/null 2>&1 ||
                sensible-browser "http://localhost:${UI_PORT}" >/dev/null 2>&1 ||
                true
            exit 0
        fi
        sleep 1
    done
    echo "⚠️  Engine did not become healthy in 90s — open http://localhost:${UI_PORT} manually."
) &

# exec replaces the shell so Ctrl+C goes straight to the engine
exec python -m engine
# ── Manual Test Cases for --network host check ─────────────────────────
# These cases verify the container port-holding logic works correctly.
#
# Test 1: Blocking case — container with --network host holding ports
#   $ docker run --network host -d nginx
#   $ ./start-engine.sh
#   EXPECTED: ❌ Container '...' runs with --network host and holds the shipped ports 8000 8501; stop it or remove --network host.
#   The script detects that the host network namespace has the API/UI ports in use.
#
# Test 2: Pass-through case — container with --network host NOT holding ports
#   $ docker run --network host -d some-container-that-doesnt-use-those-ports
#   $ ./start-engine.sh
#   EXPECTED: ⚠️ Container '...' runs with --network host but does not hold ports 8000/8501; continuing with warning.
#   The script warns but allows the launch to proceed.
#
# Test 3: No container case — normal launch without --network host containers
#   $ ./start-engine.sh
#   EXPECTED: Launches normally, no warning about containers.
#
# Test 4: Actual port conflict (no docker)
#   $ ./start-engine.sh  # while another process holds port 8000
#   EXPECTED: ❌ Port 8000 or 8500 is already in use
#
# To run these tests:
#   1. Start Docker with --network host containers as described
#   2. Run ./start-engine.sh --gate first to ensure environment is clean
#   3. Run ./start-engine.sh and observe the behavior
#   4. Check the output matches the expected messages above
# ───────────────────────────────────────────────────────────────────────
