# Agentic AI Install Guide — Serpent Circle Hype-Coin Predictive Engine

> **This file is a starter prompt.** If you are a person: copy everything
> below the line into your coding agent (or hand the agent this file) and let
> it perform the install. If you are an AI agent reading this: the text below
> is your task. Execute it step by step, verify each stage before moving on,
> and report back in the format given at the end.

---

You are installing the **Serpent Circle Hype-Coin Predictive Engine**
(https://github.com/kingcinder/Hype-Coin-Predictive-Engine) on this machine.
Work through the steps in order. Do not skip the verification steps — an
install that has not passed the health check in Step 6 is not finished.
Everything here mirrors the human guide in `INSTALL.md`; if the two ever
disagree, `INSTALL.md` and the shipped scripts win.

## Ground rules

- **Default to the desktop (local) install** in Step 3A. Use the system
  install in Step 3B only if the user explicitly asked for a systemd service.
- **Never print, log, or echo the value of `ENGINE_API_TOKEN`** or any other
  secret. The API token is passed to `curl` as a stdin config (`curl -K -`)
  precisely so it never appears in a command line or the process list —
  keep it that way.
- **Exactly one engine writer per database.** The zero-container SQLite
  profile enforces a single-writer lock at boot. Never start a second
  `python -m engine` or worker loop against the same `serpent.db`; if a
  start fails with the writer-lock or "port 8000 is already in use" message,
  an engine is already running — stop and report that instead of forcing it.
- If a step fails, stop, capture the exact error output, and report it.
  Do not improvise fixes that change the install layout (paths, service
  names, ports) without telling the user.

## Step 1 — Check the environment

Requirements (from `INSTALL.md`): Ubuntu 20.04 or newer (tested on 24.04),
**Python 3.12 or newer** (the package declares `requires-python = ">=3.12"`),
at least 2 GB RAM and 10 GB free disk.

```bash
python3 --version        # must report 3.12+
git --version
df -h .                  # confirm ~10 GB free
```

If Python 3.12 is missing, install it (this needs sudo — ask the user if
you do not have it):

```bash
sudo apt install python3.12 python3.12-venv python3.12-dev git
```

## Step 2 — Get the code

Skip this step if you are already inside a checkout of the repo.

```bash
git clone https://github.com/kingcinder/Hype-Coin-Predictive-Engine.git
cd Hype-Coin-Predictive-Engine
git branch --show-current   # expect: main
```

## Step 3A — Desktop install (default, no root)

Run the shipped installer from the repo root:

```bash
bash packaging/install-local.sh
# equivalent: make install-local
```

The installer does three things:

1. Creates the project virtualenv (`.venv`) and installs the package via
   `scripts/install_dev_deps.sh`: the pinned lock `requirements-dev.txt`
   first, then this project editable with `--no-deps`. The lock is
   authoritative, so pip never re-resolves the `pyproject.toml` floors —
   that is how SQLAlchemy 2.1 once arrived and failed the mypy gate. It
   still installs **every** dependency the project needs, including ones a
   partial manual install can miss (e.g. `defusedxml`); do not hand-pick a
   subset of packages.
2. Bootstraps the SQLite database via `scripts/bootstrap_local.py`
   (skipped automatically if `serpent.db` already exists).
3. Installs a **Serpent Circle Engine** desktop shortcut that starts the
   whole stack (worker + API + GUI).

Manual equivalent, if the installer script itself fails and you must
reproduce it by hand:

```bash
python3 -m venv .venv
bash scripts/install_dev_deps.sh .venv/bin/pip
.venv/bin/python scripts/bootstrap_local.py
```

Optional sanity check that the venv and the declared dependencies agree:

```bash
make check-deps                  # every third-party import is declared
make check-imports VENV=.venv    # imports resolve against this venv
```

## Step 3B — System install (only if the user asked for a service)

```bash
sudo bash packaging/install.sh
```

This creates a `serpent` system user, a venv at `/opt/serpent/.venv`,
runs migrations, registers and starts the `serpent.service` systemd unit,
and installs the `serpent` CLI. Manage it with `sudo serpent start|stop|
restart|status|logs|update|uninstall`. Config lives in `/opt/serpent/.env`.
(A second, split-services installer exists at `deploy/install.sh`; use it
only if the user explicitly asks for split `serpent-api` / `serpent-worker`
/ `serpent-ui` units.)

## Step 4 — Configuration (usually nothing to do)

A fresh checkout needs **no** `.env`: with no `.env` and no database URL
override, the engine selects `ENV=local-single` — SQLite at `serpent.db`,
zero containers. Copying `.env.example` to `.env` selects the documented
PostgreSQL profile instead; do that only if the user wants PostgreSQL.

- API and GUI bind to `127.0.0.1` by default (API port `8000`, GUI port
  `8501`). Leave them on loopback unless the user explicitly wants LAN
  access; a non-loopback bind requires **all** of `ENGINE_ALLOW_LAN=1`, a
  chosen `API_HOST`/`UI_HOST`, a nonempty `ENGINE_API_TOKEN`, and
  `ENGINE_API_AUTH_BYPASS=0`, and startup rejects anything less.
- Optional data-source keys (Helius, Etherscan, Farcaster, an LLM provider,
  etc.) go in `.env`. The engine runs without them; never invent values.

## Step 5 — Start the engine

Desktop install:

```bash
./start-engine.sh          # guard → verified DB backup → launch
./start-engine.sh --gate   # same, plus `make gate` before starting
# or, in a plain terminal (no guards, no backup):
.venv/bin/python -m engine        # equivalent: make engine
```

`start-engine.sh` is the one documented bring-up path. It refuses to start a
second writer (engine already running, port 8000/8501 bound, or a container
with `--network host`), snapshots `serpent.db` via SQLite's backup API into
`~/serpent-backups/` and prints the sha256, keeps only the newest
`SERPENT_BACKUP_KEEP` snapshots per database (default 10; pruned by
`scripts/prune_backups.py`), and with `--gate` runs the full check suite first.

One process runs everything: it applies migrations, then runs the
ingestion worker loop, the REST API on http://localhost:8000, and the
Streamlit GUI on http://localhost:8501. Ctrl+C stops all three cleanly.

System install: `sudo serpent start`.

## Step 6 — Verify it is actually up (required)

`/health` sits behind the same bearer <redacted> as the rest of the API.
An unauthenticated request returns **401 — that is the gate working, not
an outage.** Verify with the token, passed via stdin config so it never
touches the process list (this is the same pattern the shipped
`start-engine.sh` launcher uses):

```bash
tok="${ENGINE_API_TOKEN:-}"
if [ -z "$tok" ] && [ -f .env ]; then
  tok="$(sed -n 's/^ENGINE_API_TOKEN=//p' .env | head -1 | tr -d '"'"'"'\r')"
fi
printf 'header = "Authorization: Bearer %s"\n' "$tok" \
  | curl -sf -K - "http://localhost:8000/health"
```

Success = a JSON health response with HTTP 200. For a desktop install with
no token configured, an authenticated request may carry an empty token —
what matters is that the endpoint answers through the shipped pattern.

Then confirm the dashboard responds:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8501   # expect 200
```

For a system install, `sudo serpent status` gives a token-free status
check.

If verification fails: check the engine's terminal/log output first, then
the troubleshooting section of `INSTALL.md` (port already in use, database
lock, service logs). The retention-wedge diagnosis lives in
`docs/runbook.md`.

## Step 7 — Report back

Tell the user, in this shape:

- Which install path was used (desktop / system), and the checkout path.
- Python version and that `.venv` was created from the declared
  dependencies from the pinned lock (`scripts/install_dev_deps.sh`).
- Whether the database was bootstrapped fresh or already existed.
- The exact `/health` result (HTTP status + response body) and the
  dashboard check result — not just "it works."
- How to start/stop it from now on (`./start-engine.sh` or
  `python -m engine`; `sudo serpent …` for a system install), and where
  the GUI lives: http://localhost:8501.
- Anything that failed or was skipped, with the exact error text.
