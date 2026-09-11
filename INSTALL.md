# Installation

Two ways to install: a **desktop (local) install** that needs no root and is
ideal for running the engine from a repo checkout on your own machine, and a
**system install** that registers Serpent Circle as a systemd service on Ubuntu.

## Desktop Install (no root)

From a repo checkout:

```bash
bash packaging/install-local.sh
# or: make install-local
```

The installer:
- Creates the project virtualenv (`.venv`) and installs dependencies
- Bootstraps the SQLite database (skipped if already present)
- Installs a **Serpent Circle Engine** desktop shortcut that starts the whole
  stack (worker + API + GUI) in a terminal and auto-opens the dashboard at
  http://localhost:8501 once the engine is healthy

After install, double-click **Serpent Circle Engine** on your desktop (or run
`./start-engine.sh`). The engine also runs from a plain terminal with
`python -m engine`.

## System Install (Ubuntu service)

One command installs Serpent Circle as a system service on Ubuntu.

### Quick Install

```bash
# Clone and install
git clone https://github.com/kingcinder/Hype-Coin-Predictive-Engine.git
cd Hype-Coin-Predictive-Engine-main
sudo bash packaging/install.sh
```

Or if the project is already on your machine:

```bash
sudo bash ~/Documents/Hype-Coin-Predictive-Engine-main/packaging/install.sh
```

The installer will:
- Create a `serpent` system user
- Set up a Python virtual environment in `/opt/serpent/.venv`
- Install all dependencies
- Run database migrations automatically
- Register and start a systemd service
- Install the `serpent` CLI command

## After Install

```bash
# Start the engine
sudo serpent start

# Open the dashboard
# → http://localhost:8501
# → http://localhost:8000/health (API health check)

# View logs
sudo serpent logs

# Check status
sudo serpent status

# Stop
sudo serpent stop

# Restart
sudo serpent restart
```

## Docker & Compose (optional container deployment)

Prefer containers? The stack runs API + GUI + worker in one container, plus an
Ollama container for the local LLM layer and a backup sidecar:

```bash
# Dev stack (includes Ollama + backup sidecar)
docker compose up -d

# Production-hardened stack: read-only rootfs, dropped capabilities,
# resource limits, log rotation. Optionally create .env.prod for secrets
# (HELIUS_API_KEY, ETHERSCAN_API_KEY, NTFY_TOPIC, FARCASTER_API_KEY, ...):
cp .env .env.prod && nano .env.prod
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

- **GUI:** http://localhost:8501 · **API health:** http://localhost:8000/health
- **Ollama:** pulls `qwen2.5:0.5b` on first boot (override with `OLLAMA_MODEL`);
  models persist in the `ollama-models` volume.
- **Backups:** the sidecar snapshots SQLite + the Parquet archive daily into the
  `serpent-backups` volume with 7-day retention. Restore with
  `docker compose run --rm backup python -c "import shutil; shutil.copy('/app/backups/<snapshot>/serpent.db', '/app/data/serpent.db')"`
  (stop the engine first).
- **Data:** SQLite DB, forecast models, and the Parquet lake all live in the
  `serpent-data` volume — they survive container recreation.

## Configuration

Edit the config file:

```bash
sudo nano /opt/serpent/.env
```

Key settings:
- `env=local-single` — zero-container mode (SQLite, no Docker)
- `scan_interval_seconds=300` — how often the engine scans
- `nightcrawler_enabled=true` — enable data crawlers
- `llm_enabled=false` — set to `true` if Ollama is running locally

### Zero-container SQLite: single writer, enforced at boot

The `env=local-single` (SQLite, no Docker) profile supports exactly **one
writer** per `serpent.db`. SQLite allows only a single process to hold the
write lock at a time, and two writers contend until one wedges the loop on
`database is locked` — which is how the retention/phase wedge starts.

That constraint is now **enforced at boot** with an advisory lock: both the
monolithic engine (`python -m engine`) and the standalone worker loop
(`python -m ingestion.worker --loop`) take a non-blocking `flock` on a sibling
`<db>.lock` file and hold it for their lifetime (the OS releases it on exit or
crash, so there is never a stale lock). You may run either the monolithic
engine **or** one worker loop as the writer — but not both, and not two of
either — against the same SQLite file. The optional Postgres/container
profiles are unaffected (Postgres handles many writers itself).

If a second writer tries to start, it fails fast at boot with a clear message
and exits (code 1) instead of wedging the loop:

```text
another process already holds the SQLite writer lock (/path/to/serpent.db.lock);
refusing to start. Keep exactly one writer per serpent.db — a second writer
causes 'database is locked' loop wedges.
```

The monolithic engine also refuses to boot when the API port (`8000` by
default) is already bound — the classic symptom of a second engine already
running against the same DB:

```text
ERROR: port 8000 is already in use — another engine (or a stale server) is
likely running against the same DB. Refusing to start to avoid wedging the
loop with a second writer.
```

Either message means the DB (or the port) is already owned by another process.
Find and stop the existing owner, then start again. See the retention-wedge
diagnosis and recovery steps in [`docs/runbook.md`](docs/runbook.md).

After editing:

```bash
sudo serpent restart
```

## Update

Pulls latest code, runs migrations, restarts — no data loss:

```bash
sudo serpent update
```

## Uninstall

```bash
# Remove services and CLI (keeps database and archive data)
sudo serpent uninstall

# Remove EVERYTHING including data
sudo serpent uninstall --remove-data
```

## System Requirements

- Ubuntu 20.04+ (tested on 24.04)
- Python 3.10+ (3.12 recommended)
- 2 GB RAM minimum
- 10 GB disk space

The installer will check for Python and tell you what to install if missing:

```bash
sudo apt install python3.12 python3.12-venv python3.12-dev git
```

## Import lint gates (CI & local)

For contributors: two stdlib-only guards run in the CI lint job and as
pre-commit hooks, so a broken or undeclared import fails lint before it can
break the Tests job or a fresh install:

- `scripts/check_broken_imports.py` — fails on imports that would break
  `pytest` collection; with `--venv PATH` it also verifies third-party roots
  against that venv's site-packages.
- `scripts/check_declared_deps.py` — fails on third-party imports not
  declared in `pyproject.toml` (`[project.dependencies]` or an
  optional-dependencies group) or registered in `KNOWN_VENV_ABSENT`.

CI runs the collection guard with `--venv .venv` against the lint job's own
install venv and runs the declaration scan right after. Reproduce that full
coverage locally:

```bash
make check-imports VENV=.venv   # collection guard + venv resolution
make check-deps                 # dependency-declaration guard
```

(`make check-imports` without `VENV` uses bare `python3` — the script is
stdlib-only — so it works before `make setup`.)

**Intentional optional imports** — roots the lint venv legitimately lacks,
like the opt-in Telegram crawler's `telethon` — are registered in
`KNOWN_VENV_ABSENT`, the commented tuple at the top of
`scripts/check_broken_imports.py`; every entry carries a why-comment. Both
guards honor the registry, and parametrized guard-the-guard tests fail if a
registered row ever stops mattering.

## Troubleshooting

For incidents in a running engine — e.g. the loop wedging in the `retention`
phase (`database is locked` in every phase followed by long silence), the
single-writer lock guard, and the phase-watchdog recovery — see the
**operational runbook**: [`docs/runbook.md`](docs/runbook.md).

**Service won't start:**
```bash
sudo serpent status          # check service state
sudo serpent logs-recent     # last 50 lines of logs
sudo journalctl -u serpent.service -n 100  # detailed logs
```

**Port already in use:**
```bash
sudo lsof -i :8000  # find what's using port 8000
sudo lsof -i :8501  # find what's using port 8501
sudo serpent stop    # stop the service
```
The engine now refuses to boot when port 8000 is already bound (it likely
means a second engine is running against the same DB and would wedge the loop
as a second writer). See the retention-wedge diagnosis in
[`docs/runbook.md`](docs/runbook.md).

**Database issues:**
```bash
cd /opt/serpent
sudo -u serpent .venv/bin/python -c "
from storage.database import Base, engine
from storage import models
Base.metadata.create_all(bind=engine)
print('Schema OK')
"
```
If the worker logs `database is locked` repeatedly or falls silent for hours
(stuck in a phase), that is the retention/phase wedge — the runbook
[`docs/runbook.md`](docs/runbook.md) has the diagnosis and recovery steps.

**Reinstall from scratch:**
```bash
sudo serpent uninstall --remove-data
sudo bash /path/to/Hype-Coin-Predictive-Engine-main/packaging/install.sh
```
