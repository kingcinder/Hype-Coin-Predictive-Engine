#!/usr/bin/env bash
set -euo pipefail

# Shortcuts for managing the Serpent Circle Hype-Coin Engine
# Usage: ./scripts/dev.sh <command>

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMMAND="${1:-help}"

usage() {
    cat <<EOF
Serpent Circle Hype-Coin Engine - Development Shortcuts

Usage: ./scripts/dev.sh <command>

OPEN (Start Services):
  api             Start the REST API server
  ui              Start the Streamlit dashboard
  worker          Run one ingestion scan
  engine          Start the full engine (all-in-one)
  docker-up       Start all services via Docker Compose
  docker-down     Stop all Docker services

INSTALL:
  install         Install the project in development mode
  install-dev     Install with dev dependencies
  install-telegram Install with Telegram support

UPDATE:
  update          Update all dependencies to latest compatible versions
  update-dev      Update dev dependencies
  migrate         Run database migrations

UNINSTALL:
  uninstall       Uninstall the project (editable mode)
  clean           Remove build artifacts and caches
  clean-db        Remove local SQLite database

DEVELOPMENT:
  test            Run all tests
  smoke           Run quick smoke tests
  lint            Run linter and type checker
  format          Auto-format code
  seed            Seed the database with fixtures

DATABASE:
  bootstrap-local Bootstrap local SQLite environment
  archive         Run evidence archival
  archive-query   Query the Parquet lake (arg: SQL query)
  retention       Run one retention-autopilot pass (compact + prune + report)
  parity          Run one lake-vs-SQL parity check (daily CI job)
  lifecycle-backtest Run lifecycle backtest (requires START, FORWARD_HOURS)

OTHER:
  refresh-rpc     Refresh RPC endpoint pools
  backtest        Run backtest (requires START, FORWARD_HOURS; optional FEATURE_SOURCE=sql|lake)
  forecast-ab     Run forecast A/B experiment
  help            Show this help message

Examples:
  ./scripts/dev.sh install          # Install project
  ./scripts/dev.sh api              # Start API server
  ./scripts/dev.sh engine           # Start everything
  ./scripts/dev.sh docker-up        # Start with Docker
  ./scripts/dev.sh test             # Run tests
  ./scripts/dev.sh clean            # Clean up artifacts
  ./scripts/dev.sh uninstall        # Remove project
EOF
}

# ── Open Commands ────────────────────────────────────────────────────────────

cmd_api() {
    echo "Starting API server on http://localhost:8000 ..."
    uvicorn api.main:app --host 0.0.0.0 --port 8000
}

cmd_ui() {
    echo "Starting Streamlit dashboard on http://localhost:8501 ..."
    streamlit run ui/app.py --server.port=8501
}

cmd_worker() {
    echo "Running one ingestion scan..."
    python -m ingestion.worker --once
}

cmd_engine() {
    echo "Starting full engine (worker + API + UI)..."
    python -m engine
}

cmd_docker_up() {
    echo "Starting all services via Docker Compose..."
    docker compose up --build
}

cmd_docker_down() {
    echo "Stopping Docker services..."
    docker compose down
}

# ── Install Commands ─────────────────────────────────────────────────────────

cmd_install() {
    echo "Installing project in development mode..."
    python -m pip install --upgrade pip
    python -m pip install -e .
    echo "✓ Project installed successfully"
}

cmd_install_dev() {
    echo "Installing project with dev dependencies..."
    python -m pip install --upgrade pip
    python -m pip install -e ".[dev]"
    echo "✓ Project installed with dev dependencies"
}

cmd_install_telegram() {
    echo "Installing project with Telegram support..."
    python -m pip install --upgrade pip
    python -m pip install -e ".[telegram]"
    echo "✓ Project installed with Telegram support"
}

# ── Update Commands ──────────────────────────────────────────────────────────

cmd_update() {
    echo "Updating all dependencies..."
    python -m pip install --upgrade pip
    python -m pip install --upgrade -e .
    echo "✓ Dependencies updated"
}

cmd_update_dev() {
    echo "Updating dev dependencies..."
    python -m pip install --upgrade pip
    python -m pip install --upgrade -e ".[dev]"
    echo "✓ Dev dependencies updated"
}

cmd_migrate() {
    echo "Running database migrations..."
    alembic -c storage/alembic.ini upgrade head
    echo "✓ Migrations complete"
}

# ── Uninstall Commands ───────────────────────────────────────────────────────

cmd_uninstall() {
    echo "Uninstalling project..."
    python -m pip uninstall -y serpent-hype-coin-engine
    echo "✓ Project uninstalled"
}

cmd_clean() {
    echo "Cleaning build artifacts and caches..."
    rm -rf build/ dist/ *.egg-info/ .eggs/
    rm -rf __pycache__/ .pytest_cache/ .mypy_cache/ .ruff_cache/
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete 2>/dev/null || true
    echo "✓ Cleanup complete"
}

cmd_clean_db() {
    # The SQLite file is the primary data store: deleting it is unrecoverable,
    # so a bare invocation asks first. Pass --force/-y to skip the prompt
    # (for scripted use).
    local force=false
    for arg in "$@"; do
        [[ "$arg" == "--force" || "$arg" == "-y" ]] && force=true
    done
    if [[ "$force" != "true" ]]; then
        echo "⚠️  This will PERMANENTLY DELETE the local SQLite database"
        echo "   (serpent.db + WAL files). This cannot be undone."
        confirm=""
        # A closed stdin (EOF) must abort, not fall through: read returns
        # non-zero on EOF, which set -e would otherwise turn into a bare
        # exit before the abort message prints.
        read -r -p "Type 'yes' to continue: " confirm || true
        if [[ "$confirm" != "yes" ]]; then
            echo "Aborted."
            return 0
        fi
    fi
    echo "Removing local SQLite database..."
    if [[ -f serpent.db ]]; then
        rm -f serpent.db serpent.db-wal serpent.db-shm
        echo "✓ Database removed"
    else
        echo "No local database found"
    fi
}

# ── Development Commands ─────────────────────────────────────────────────────

cmd_test() {
    echo "Running all tests..."
    python -m pytest "$@"
    echo "✓ Tests passed"
}

cmd_smoke() {
    echo "Running unit smoke tests..."
    python -m pytest tests/test_schema.py tests/test_risk_scoring.py tests/test_backtest.py -q
    python -m py_compile ui/app.py

    echo "Running clean-clone startup and launcher acceptance..."
    local smoke_root clone branch api_port ui_port engine_pid="" token unauth_code health_code gui_code scores_code
    smoke_root="$(mktemp -d -t serpent-smoke.XXXXXX)"
    clone="$smoke_root/repo"
    branch="$(git branch --show-current)"
    mkdir -p "$smoke_root/home" "$smoke_root/bin"

    cleanup_smoke() {
        if [[ -n "$engine_pid" ]] && kill -0 "$engine_pid" 2>/dev/null; then
            kill -INT "$engine_pid" 2>/dev/null || true
            for _ in {1..20}; do
                kill -0 "$engine_pid" 2>/dev/null || break
                sleep 1
            done
            kill -TERM "$engine_pid" 2>/dev/null || true
            wait "$engine_pid" 2>/dev/null || true
        fi
        rm -rf "$smoke_root"
    }
    trap cleanup_smoke EXIT

    git clone --quiet --no-hardlinks --single-branch --branch "$branch" "$ROOT" "$clone"
    (
        cd "$clone"
        python3.12 -m venv .venv
        .venv/bin/pip install -q -e ".[dev]"
        ENV=local-single SERPENT_DB_PATH="$clone/serpent.db" \
            .venv/bin/python scripts/seed_fixtures.py
    )

    api_port="$(python3.12 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    ui_port="$(python3.12 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    while [[ "$api_port" == "$ui_port" ]]; do
        ui_port="$(python3.12 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    done

    # A fresh environment has no .env, token, or DB override. Only ports and
    # temporary paths are supplied to avoid colliding with the operator's stack.
    pushd "$clone" >/dev/null
    env -i \
        HOME="$smoke_root/home" \
        PATH="$clone/.venv/bin:/usr/bin:/bin" \
        API_HOST=127.0.0.1 API_PORT="$api_port" \
        API_BASE_URL="http://127.0.0.1:$api_port" \
        UI_HOST=127.0.0.1 UI_PORT="$ui_port" \
        ARCHIVE_BACKEND=local ARCHIVE_LOCAL_DIR="$clone/data/archive" \
        bash -c 'source .venv/bin/activate; exec python -m engine' \
        >"$smoke_root/direct-engine.log" 2>&1 &
    engine_pid=$!
    popd >/dev/null

    token=""
    for _ in {1..90}; do
        if [[ -f "$clone/.env" ]]; then
            token="$(sed -n 's/^ENGINE_API_TOKEN=//p' "$clone/.env" | tail -n 1)"
        fi
        if [[ -n "$token" ]] && curl -sS -o "$smoke_root/health.json" \
            -w '%{http_code}' -H "Authorization: Bearer $token" \
            "http://127.0.0.1:$api_port/health" >"$smoke_root/health.code" 2>/dev/null; then
            health_code="$(cat "$smoke_root/health.code")"
            [[ "$health_code" == "200" ]] && break
        fi
        sleep 1
    done
    [[ -n "$token" ]] || { echo "FAIL: first-run API token was not provisioned"; return 1; }
    [[ "$(stat -c '%a' "$clone/.env")" == "600" ]] || {
        echo "FAIL: first-run .env permissions are not 0600"; return 1;
    }
    [[ "${health_code:-}" == "200" ]] || { echo "FAIL: authorized /health did not return 200"; return 1; }
    unauth_code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$api_port/health")"
    [[ "$unauth_code" == "401" || "$unauth_code" == "403" ]] || {
        echo "FAIL: unauthenticated /health returned $unauth_code"; return 1;
    }
    for _ in {1..90}; do
        gui_code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$ui_port/" 2>/dev/null || true)"
        if [[ "$gui_code" == "200" ]] && python - "$smoke_root/health.json" <<'PY'
import json, sys
components = {row["component"]: row["state"] for row in json.load(open(sys.argv[1]))["components"]}
assert components.get("data_lake") == "ok"
assert components.get("parity") != "red"
PY
        then
            break
        fi
        sleep 1
        curl -sS -o "$smoke_root/health.json" -H "Authorization: Bearer $token" \
            "http://127.0.0.1:$api_port/health" || true
    done
    [[ "${gui_code:-}" == "200" ]] || { echo "FAIL: GUI returned ${gui_code:-no response}"; return 1; }
    scores_code="$(curl -sS -o "$smoke_root/scores.json" -w '%{http_code}' \
        -H "Authorization: Bearer $token" "http://127.0.0.1:$api_port/scores/top?limit=20")"
    [[ "$scores_code" == "200" ]] && rg -q 'HYPE|DANGER' "$smoke_root/scores.json" || {
        echo "FAIL: seeded score data was not served"; return 1;
    }
    python - "$smoke_root/health.json" <<'PY'
import json, sys
components = {row["component"]: row["state"] for row in json.load(open(sys.argv[1]))["components"]}
assert components.get("data_lake") == "ok", components.get("data_lake")
assert components.get("parity") != "red", components.get("parity")
PY
    echo "PASS: token mode 0600; /health authorized=$health_code unauthenticated=$unauth_code; seeded scores=$scores_code; GUI=$gui_code; data_lake=ok; parity not red"
    kill -INT "$engine_pid"
    for _ in {1..30}; do
        kill -0 "$engine_pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$engine_pid" 2>/dev/null; then
        echo "FAIL: engine did not stop after SIGINT"
        return 1
    fi
    wait "$engine_pid"
    engine_pid=""
    echo "PASS: engine shut down cleanly on SIGINT"

    cat >"$smoke_root/bin/xdg-open" <<'SH'
#!/bin/sh
printf '%s' "$1" >"$SMOKE_OPEN_LOG"
SH
    chmod +x "$smoke_root/bin/xdg-open"
    : >"$smoke_root/opened-url"
    pushd "$clone" >/dev/null
    env -i \
        HOME="$smoke_root/home" \
        PATH="$smoke_root/bin:$clone/.venv/bin:/usr/bin:/bin" \
        SMOKE_OPEN_LOG="$smoke_root/opened-url" \
        API_HOST=127.0.0.1 API_PORT="$api_port" API_BASE_URL="http://127.0.0.1:$api_port" \
        UI_HOST=127.0.0.1 UI_PORT="$ui_port" \
        SERPENT_API_PORT="$api_port" SERPENT_UI_PORT="$ui_port" \
        ARCHIVE_BACKEND=local ARCHIVE_LOCAL_DIR="$clone/data/archive" \
        ./start-engine.sh >"$smoke_root/launcher.log" 2>&1 &
    engine_pid=$!
    popd >/dev/null
    for _ in {1..90}; do
        [[ -s "$smoke_root/opened-url" ]] && break
        sleep 1
    done
    [[ "$(cat "$smoke_root/opened-url")" == "http://localhost:$ui_port" ]] || {
        echo "FAIL: launcher did not open the expected GUI URL"; return 1;
    }
    echo "PASS: launcher opened http://localhost:$ui_port"
    kill -INT "$engine_pid"
    for _ in {1..30}; do
        kill -0 "$engine_pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$engine_pid" 2>/dev/null; then
        echo "FAIL: launcher engine did not stop after SIGINT"
        return 1
    fi
    wait "$engine_pid"
    engine_pid=""
    trap - EXIT
    cleanup_smoke
    echo "✓ Smoke tests passed"
}

cmd_lint() {
    echo "Running linter..."
    ruff check .
    echo "Running type checker..."
    mypy catalyst common forecast mempool narrative ops pump_physics storage ingestion features fingerprint radar risk_engine scoring backtest api
    echo "✓ Lint passed"
}

cmd_format() {
    echo "Auto-formatting code..."
    ruff format .
    ruff check --fix .
    echo "✓ Code formatted"
}

cmd_seed() {
    echo "Seeding database with fixtures..."
    python -m storage.seed
    python scripts/seed_fixtures.py
    echo "✓ Database seeded"
}

# ── Database Commands ────────────────────────────────────────────────────────

cmd_bootstrap_local() {
    echo "Bootstrapping local SQLite environment..."
    python scripts/bootstrap_local.py
    echo "✓ Local environment ready"
}

cmd_archive() {
    echo "Running evidence archival..."
    python -m ops.archive --once
    echo "✓ Archival complete"
}

cmd_retention() {
    echo "Running one retention-autopilot pass..."
    python -m ops.retention --once
    echo "✓ Retention pass complete"
}

cmd_parity() {
    echo "Running lake-vs-SQL parity check..."
    python -m ops.parity --once
    echo "✓ Parity check complete"
}

cmd_archive_query() {
    local sql="${1:-${SQL:-}}"
    if [[ -z "$sql" ]]; then
        echo "Error: Provide SQL query as argument or via SQL environment variable"
        echo "Usage: ./scripts/dev.sh archive-query 'SELECT ...'"
        echo "   or: SQL='SELECT ...' ./scripts/dev.sh archive-query"
        exit 1
    fi
    python -m ops.archive --query "$sql"
}

cmd_lifecycle_backtest() {
    if [[ -z "${START:-}" ]] || [[ -z "${FORWARD_HOURS:-}" ]]; then
        echo "Error: Set START and FORWARD_HOURS environment variables"
        echo "Usage: START=2026-05-01T00:00:00Z FORWARD_HOURS=24 ./scripts/dev.sh lifecycle-backtest"
        exit 1
    fi
    python -m pump_physics.backtest --start "$START" --forward-hours "$FORWARD_HOURS"
}

# ── Other Commands ───────────────────────────────────────────────────────────

cmd_refresh_rpc() {
    echo "Refreshing RPC endpoint pools..."
    python scripts/refresh_rpc_pools.py
    echo "✓ RPC pools refreshed"
}

cmd_backtest() {
    if [[ -z "${START:-}" ]] || [[ -z "${FORWARD_HOURS:-}" ]]; then
        echo "Error: Set START and FORWARD_HOURS environment variables"
        echo "Usage: START=2026-05-01T00:00:00Z FORWARD_HOURS=24 ./scripts/dev.sh backtest"
        exit 1
    fi
    local feature_source="${FEATURE_SOURCE:-}"
    if [[ -n "$feature_source" ]]; then
        python -m backtest.runner --start "$START" --forward-hours "$FORWARD_HOURS" --feature-source "$feature_source"
    else
        python -m backtest.runner --start "$START" --forward-hours "$FORWARD_HOURS"
    fi
}

cmd_forecast_ab() {
    echo "Running forecast A/B experiment..."
    python -m forecast.experiment
    echo "✓ Forecast A/B complete"
}

# ── Main Dispatch ────────────────────────────────────────────────────────────

case "$COMMAND" in
    # Open
    api)                cmd_api ;;
    ui)                 cmd_ui ;;
    worker)             cmd_worker ;;
    engine)             cmd_engine ;;
    docker-up)          cmd_docker_up ;;
    docker-down)        cmd_docker_down ;;
    
    # Install
    install)            cmd_install ;;
    install-dev)        cmd_install_dev ;;
    install-telegram)   cmd_install_telegram ;;
    
    # Update
    update)             cmd_update ;;
    update-dev)         cmd_update_dev ;;
    migrate)            cmd_migrate ;;
    
    # Uninstall
    uninstall)          cmd_uninstall ;;
    clean)              cmd_clean ;;
    clean-db)           shift; cmd_clean_db "$@" ;;
    
    # Development
    test)               cmd_test ;;
    smoke)              cmd_smoke ;;
    lint)               cmd_lint ;;
    format)             cmd_format ;;
    seed)               cmd_seed ;;
    
    # Database
    bootstrap-local)    cmd_bootstrap_local ;;
    archive)            cmd_archive ;;
    archive-query)      cmd_archive_query ;;
    retention)          cmd_retention ;;
    parity)             cmd_parity ;;
    
    # Other
    refresh-rpc)        cmd_refresh_rpc ;;
    lifecycle-backtest) cmd_lifecycle_backtest ;;
    backtest)           cmd_backtest ;;
    forecast-ab)        cmd_forecast_ab ;;
    
    help|--help|-h)     usage ;;
    
    *)
        echo "Error: Unknown command '$COMMAND'"
        echo "Run './scripts/dev.sh help' for usage information"
        exit 1
        ;;
esac
