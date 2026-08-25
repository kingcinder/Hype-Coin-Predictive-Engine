# Installation

One command installs Serpent Circle as a system service on Ubuntu.

## Quick Install

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

## Troubleshooting

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

**Reinstall from scratch:**
```bash
sudo serpent uninstall --remove-data
sudo bash /path/to/Hype-Coin-Predictive-Engine-main/packaging/install.sh
```
