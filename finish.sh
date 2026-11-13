#!/usr/bin/env bash
# Serpent Circle Hype-Coin engine bring-up.
# Prompt: HYPE_COIN_FINISH_AND_LAUNCH.md, with the amendments from the run review:
#   - install from requirements-dev.txt then `pip install -e . --no-deps`
#   - run the gate with any root .env moved aside and restored with a checksum
#   - stop the engine with SIGKILL (SIGINT wedges on this revision)
#   - lock-error criterion relaxed to "at most two boot warnings, no growth afterwards"
#   - refuse to start if another container holds the shipped API/GUI ports
set -uo pipefail

BK_DIR="$HOME/serpent-backups"
LOG_DIR="$HOME/serpent-logs"
mkdir -p "$BK_DIR" "$LOG_DIR"

die() { echo "STOP[$2]: $1" >&2; exit "$2"; }

# ── S0: locate the checkout ────────────────────────────────────────────
ROOT=""
for d in "${SERPENT_PROJECT_DIR:-}" \
         "$HOME/Documents/Hype-Coin-Predictive-Engine-main" \
         "$HOME/Documents/Hype-Coin-Predictive-Engine" \
         "$PWD"; do
  if [ -n "$d" ] && [ -f "$d/pyproject.toml" ] && grep -q 'serpent-hype-coin-engine' "$d/pyproject.toml"; then
    ROOT="$d"; break
  fi
done
[ -n "$ROOT" ] || die "checkout not found (set SERPENT_PROJECT_DIR)" 10
cd "$ROOT" || die "cannot cd to $ROOT" 10
echo "CHECKOUT=$ROOT"

# ── S1: single-writer guard ────────────────────────────────────────────
if pgrep -f '[p]ython.* -m engine' >/dev/null; then
  pgrep -af '[p]ython.* -m engine' >&2
  die "an engine process is already running; do not start a second" 11
fi
if ss -ltn 2>/dev/null | grep -Eq ':(8000|8501)[[:space:]]'; then
  ss -ltnp 2>/dev/null | grep -E ':(8000|8501)[[:space:]]' >&2 || true
  die "port 8000 or 8501 already in use" 12
fi
# Amendment: a host-network container can own the shipped port without showing in
# `docker ps` PORT column, so check for one explicitly.
if command -v docker >/dev/null 2>&1; then
  for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
    net="$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$c" 2>/dev/null)"
    [ "$net" = "host" ] && die "container '$c' is running with --network host and may hold 8000/8501" 12
  done
fi

# ── S2: sync to main without rewriting anything ────────────────────────
[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || { git status --short >&2; die "tracked files are modified; refusing to pull over them" 13; }
git fetch origin main || die "git fetch failed" 18
git checkout main || die "git checkout main failed" 18
git merge --ff-only origin/main || die "local history diverged from origin/main; report, do not force" 18
echo "HEAD=$(git rev-parse --short HEAD)"

# ── S3: back up existing data (read-only copy via SQLite backup API) ───
DB_BACKUP="none (no serpent.db present)"
if [ -s serpent.db ]; then
  DB_BACKUP="$BK_DIR/serpent-$(date +%Y%m%d-%H%M%S).db"
  DB_BACKUP="$DB_BACKUP" python3 - <<'PY' || die "sqlite backup failed" 19
import os, sqlite3
src = sqlite3.connect("file:serpent.db?mode=ro", uri=True)
dst = sqlite3.connect(os.environ["DB_BACKUP"])
src.backup(dst)
dst.close(); src.close()
PY
  DB_SHA="$(cd "$BK_DIR" && sha256sum "$(basename "$DB_BACKUP")" | awk '{print $1}')"
  echo "DB_BACKUP_SHA256=$DB_SHA"
fi
echo "DB_BACKUP=$DB_BACKUP"
DB_SHA="${DB_SHA:-none}"

# ── S4: complete venv on Python 3.12+ from the shipped lock ────────────
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' \
  || die "python3 is older than 3.12" 14
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
  mv .venv ".venv.old-$(date +%s)"
fi
[ -x .venv/bin/python ] || python3 -m venv .venv || die "venv creation failed" 14
.venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1
# Amendment: the unpinned editable install resolves newer majors than the lock,
# and SQLAlchemy 2.1.8 makes `mypy api/main.py:508` fail. Install the lock first.
.venv/bin/python -m pip install -r requirements-dev.txt \
  || die "pip install -r requirements-dev.txt failed" 15
.venv/bin/python -m pip install -e . --no-deps \
  || die "pip install -e . --no-deps failed" 15
.venv/bin/python -c "import defusedxml, fastapi, streamlit, sqlalchemy, alembic; print('IMPORTS_OK')" \
  || die "declared dependencies not importable after install" 15
echo "SQLALCHEMY=$(.venv/bin/python -c 'import sqlalchemy; print(sqlalchemy.__version__)')"

# ── S5: required gates, with the root .env set aside and restored ──────
restore_env() { [ -f "$ROOT/.env.gate-aside" ] && mv -f "$ROOT/.env.gate-aside" "$ROOT/.env"; }
trap restore_env EXIT INT TERM HUP
ENV_SHA_BEFORE="$(sha256sum .env 2>/dev/null | awk '{print $1}')"
[ -f .env ] && mv .env .env.gate-aside
export PATH="$ROOT/.venv/bin:$PATH"
make VENV=.venv gate > "$LOG_DIR/gate.log" 2>&1
GATE_STATUS=$?
restore_env
trap - EXIT INT TERM HUP
ENV_SHA_AFTER="$(sha256sum .env 2>/dev/null | awk '{print $1}')"
[ "$ENV_SHA_BEFORE" = "$ENV_SHA_AFTER" ] || die ".env was not restored byte-identical" 16
echo "ENV_SHA_MATCH=yes"
if [ "$GATE_STATUS" != 0 ]; then
  grep -E '^(PASS|FAIL):' "$LOG_DIR/gate.log" >&2 || true
  tail -25 "$LOG_DIR/gate.log" >&2 || true
  die "make gate failed (exit $GATE_STATUS); log: $LOG_DIR/gate.log" 17
fi
echo "GATE=PASS"
COVERAGE="$(grep -E '^TOTAL' "$LOG_DIR/gate.log" | tail -1)"

# ── S6: start the engine as a plain background process ─────────────────
setsid nohup .venv/bin/python -m engine >"$LOG_DIR/engine.log" 2>&1 &
ENGINE_PID=$!
echo "$ENGINE_PID" >"$LOG_DIR/engine.pid"

stop_engine() { kill -KILL "$ENGINE_PID" 2>/dev/null || true; }
fail() {
  echo "STOP: $1" >&2
  echo "---- last 60 log lines ----" >&2
  tail -n 60 "$LOG_DIR/engine.log" >&2 || true
  stop_engine
  exit 20
}

probe_health() {
  local tok="${ENGINE_API_TOKEN:-}"
  if [ -z "$tok" ] && [ -f .env ]; then
    tok="$(sed -n 's/^ENGINE_API_TOKEN=//p' .env | tail -n 1 | tr -d "\"'" | tr -d '\r')"
  fi
  printf 'header = "Authorization: Bearer %s"\n' "$tok" | curl -sf -K - "http://localhost:8000/health"
}

HEALTH_BODY=""
for _ in $(seq 1 180); do
  kill -0 "$ENGINE_PID" 2>/dev/null || fail "engine process exited during startup"
  if HEALTH_BODY="$(probe_health)"; then break; fi
  HEALTH_BODY=""
  sleep 1
done
[ -n "$HEALTH_BODY" ] || fail "authenticated /health did not return 200 within 180s"

UNAUTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health || true)"
GUI_CODE="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8501 || true)"
[ "$GUI_CODE" = "200" ] || fail "GUI returned $GUI_CODE, expected 200"

# Stability window. Lock criterion (amended): at most two boot warnings, no growth.
sleep 45
kill -0 "$ENGINE_PID" 2>/dev/null || fail "engine died within 45s of becoming healthy"
LOCK_EARLY="$(grep -ci 'database is locked' "$LOG_DIR/engine.log" || true)"
sleep 75
kill -0 "$ENGINE_PID" 2>/dev/null || fail "engine died inside the stability window"
LOCK_LATE="$(grep -ci 'database is locked' "$LOG_DIR/engine.log" || true)"
TRACEBACKS="$(grep -c 'Traceback' "$LOG_DIR/engine.log" || true)"
[ "$LOCK_LATE" -le 2 ] || fail "lock warnings grew past two: early=$LOCK_EARLY late=$LOCK_LATE"
[ "$LOCK_EARLY" = "$LOCK_LATE" ] || fail "lock warnings still growing: early=$LOCK_EARLY late=$LOCK_LATE"
[ "$TRACEBACKS" = "0" ] || fail "tracebacks in engine log: $TRACEBACKS"
HEALTH_FINAL="$(probe_health >/dev/null 2>&1 && echo 200 || echo 0)"

# CI status on origin/main (informational)
curl -s 'https://api.github.com/repos/kingcinder/Hype-Coin-Predictive-Engine/actions/runs?branch=main&per_page=3' \
  -o "$LOG_DIR/ci.json" || true
CI_STATUS="$(python3 - "$LOG_DIR/ci.json" <<'PY' 2>/dev/null || echo unavailable
import json, sys
try:
    runs = json.load(open(sys.argv[1])).get("workflow_runs", [])
    print("; ".join("%s@%s=%s" % (r["name"], r["head_sha"][:7], r["conclusion"] or r["status"]) for r in runs) or "unavailable")
except Exception:
    print("unavailable")
PY
)"

cat <<EOF
=============== RESULT ===============
checkout:        $ROOT
head:            $(git rev-parse --short HEAD)
python:          $(.venv/bin/python --version 2>&1)
db_backup:       $DB_BACKUP
db_backup_sha:   $DB_SHA
gate:            PASS ($COVERAGE)
engine_pid:      $ENGINE_PID
health_status:   200
unauth_status:   $UNAUTH_CODE   (401 = bearer gate working)
gui_status:      $GUI_CODE
health_final:    $HEALTH_FINAL
log_tracebacks:  $TRACEBACKS
log_lock_errors: $LOCK_LATE (early sample $LOCK_EARLY, no growth)
github_ci_main:  $CI_STATUS
engine_log:      $LOG_DIR/engine.log
stop:            kill -KILL \$(cat $LOG_DIR/engine.pid)
restart:         cd $ROOT && .venv/bin/python -m engine
gui:             http://localhost:8501
======================================
EOF
