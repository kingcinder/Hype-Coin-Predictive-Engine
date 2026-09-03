.PHONY: setup up down migrate seed worker api ui engine test lint format smoke \
	bootstrap-local local-worker local-api local-ui archive archive-query retention parity backtest refresh-rpc-pools forecast-ab install-local \
	check-imports check-deps rescore-compare score-drift score-drift-history

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

# Run one score-distribution drift probe: compare the persisted risk
# distribution the GUI serves against the current scoring formula over the
# sampled latest-decision window (KS D + distinct ratio + mean |delta|).
# --strict exits non-zero when drift is detected (CI integration); after a
# red probe, `make score-drift AUTO_APPLY=1` runs the rescore write pass —
# but only when an operator has acked the score_drift alert. See the runbook
# "Score-distribution drift" section for the full panel → review → ack →
# rescue flow.
score-drift:
	python -m ops.score_drift --once \
		$(if $(STRICT),--strict,) \
		$(if $(AUTO_APPLY),--auto-apply,)

# Print the recorded drift-probe trend series (newest first): run_ts, state,
# KS D/p, distinct ratio, mean |delta| per probe — the same rows the GUI trend
# chart and /score-drift/history serve. Read-only; no probe is run. LIMIT caps
# the rows (default 20). Use it to watch divergence grow between rescues.
score-drift-history:
	python -m ops.score_drift --history \
		$(if $(LIMIT),--limit $(LIMIT),)

refresh-rpc-pools:
	python scripts/refresh_rpc_pools.py

backtest:
	python -m backtest.runner --start "$(START)" --forward-hours "$(FORWARD_HOURS)" \
		$(if $(FEATURE_SOURCE),--feature-source "$(FEATURE_SOURCE)",)

# Review rescore movers before the write pass: print old → new risk per token
# with a mover-magnitude sweep, filtered by symbol, capped by --limit, and/or
# exported to CSV. Review flags imply --compare (→ dry-run), so this NEVER
# writes — the real migration is a bare `python scripts/rescore.py`.
# Examples:
#   make rescore-compare                          # top 50 movers by |delta|
#   make rescore-compare MIN_CHANGE=10            # only |delta| >= 10
#   make rescore-compare SYMBOL_FILTER=UNKNOWN    # only one symbol family
#   make rescore-compare LIMIT=100 TOP_PCT=10     # top 10% of movers, cap 100
#   make rescore-compare SWEEP=1 EXPORT_CSV=/tmp/movers.csv
rescore-compare:
	python scripts/rescore.py --compare \
		$(if $(MIN_CHANGE),--min-change $(MIN_CHANGE),) \
		$(if $(LIMIT),--limit $(LIMIT),) \
		$(if $(SYMBOL_FILTER),--symbol-filter $(SYMBOL_FILTER),) \
		$(if $(TOP_PCT),--top-pct $(TOP_PCT),) \
		$(if $(SWEEP),--sweep,) \
		$(if $(EXPORT_CSV),--export-csv $(EXPORT_CSV),)

lifecycle-backtest:
	python -m pump_physics.backtest --start "$(START)" --forward-hours "$(FORWARD_HOURS)"

test:
	pytest

lint:
	ruff check .
	mypy catalyst common forecast mempool narrative ops pump_physics storage ingestion features fingerprint radar risk_engine scoring backtest api
	python3 scripts/check_broken_imports.py $(if $(VENV),--venv $(VENV),)
	python3 scripts/check_declared_deps.py

# Collection guard: fail fast on dangling repo-local imports (see CI lint job).
# `python3` (not the venv python) on purpose: the script is stdlib-only, so it
# works even before `make setup`. After `make setup`, pass VENV=.venv to also
# verify third-party imports against the venv — a missing dependency then fails
# this target, matching the CI lint job's --venv contract.
VENV ?=
check-imports:
	python3 scripts/check_broken_imports.py $(if $(VENV),--venv $(VENV),)

# Dependency-declaration guard: every third-party import root must be declared
# in pyproject.toml ([project.dependencies] or an optional-dependencies group)
# or registered in KNOWN_VENV_ABSENT. Stdlib-only (tomllib parses pyproject),
# so it also runs pre-`make setup`; see the CI lint job + pre-commit hook.
check-deps:
	python3 scripts/check_declared_deps.py

format:
	ruff format .
	ruff check --fix .

smoke:
	powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 smoke
