.PHONY: setup up down migrate seed worker api ui engine test lint format smoke \
	bootstrap-local local-worker local-api local-ui archive archive-query retention parity backtest refresh-rpc-pools forecast-ab install-local

setup:
	python -m pip install --upgrade pip
	python -m pip install -e ".[dev]"

# Wire this checkout for desktop use (venv + DB bootstrap + desktop shortcut),
# no root required. See packaging/install-local.sh.
install-local:
	bash packaging/install-local.sh

up:
	docker compose up --build

down:
	docker compose down

migrate:
	alembic -c storage/alembic.ini upgrade head

seed:
	python -m storage.seed
	python scripts/seed_fixtures.py

worker:
	python -m ingestion.worker --once

api:
	uvicorn api.main:app --host 0.0.0.0 --port 8000

ui:
	streamlit run ui/app.py --server.port=8501

# Single-command engine: one process runs bootstrap + worker loop + API + GUI.
engine:
	python -m engine

# Zero-container profile: SQLite + local Parquet archive, no containers.
bootstrap-local:
	python scripts/bootstrap_local.py

local-worker:
	ENV=local-single python -m ingestion.worker --loop

local-api:
	ENV=local-single uvicorn api.main:app --host 0.0.0.0 --port 8000

local-ui:
	ENV=local-single streamlit run ui/app.py --server.port=8501

archive:
	python -m ops.archive --once

archive-query:
	python -m ops.archive --query "$(SQL)"

# Run one retention-autopilot pass (compaction + pruning + lake-growth report).
retention:
	python -m ops.retention --once

# Diagnose the retention-phase wedge (runs/health gap + competing writer/port).
diagnose-retention:
	python -m scripts.diagnose_retention

# Run one lake-vs-SQL parity check over the archived lake (daily CI job).
parity:
	python -m ops.parity --once

refresh-rpc-pools:
	python scripts/refresh_rpc_pools.py

backtest:
	python -m backtest.runner --start "$(START)" --forward-hours "$(FORWARD_HOURS)" \
		$(if $(FEATURE_SOURCE),--feature-source "$(FEATURE_SOURCE)",)

forecast-ab:
	python -m forecast.experiment $(if $(DECISION_TS),--decision-ts "$(DECISION_TS)",)

lifecycle-backtest:
	python -m pump_physics.backtest --start "$(START)" --forward-hours "$(FORWARD_HOURS)"

test:
	pytest

lint:
	ruff check .
	mypy catalyst common forecast mempool narrative ops pump_physics storage ingestion features fingerprint radar risk_engine scoring backtest api

format:
	ruff format .
	ruff check --fix .

smoke:
	powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 smoke
