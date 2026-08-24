param(
    [Parameter(Position = 0)]
    [ValidateSet("setup", "test", "smoke", "migrate", "seed", "worker", "api", "ui", "bootstrap-local", "archive", "retention", "parity", "backtest", "forecast-ab", "refresh-rpc-pools")]
    [string]$Command = "test"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

switch ($Command) {
    "setup" {
        python -m pip install --upgrade pip
        python -m pip install -e ".[dev]"
    }
    "test" {
        python -m pytest
    }
    "smoke" {
        python -m pytest tests/test_schema.py tests/test_risk_scoring.py tests/test_backtest.py
        python -m py_compile ui/app.py
    }
    "migrate" {
        alembic -c storage/alembic.ini upgrade head
    }
    "seed" {
        python -m storage.seed
        python scripts/seed_fixtures.py
    }
    "worker" {
        python -m ingestion.worker --once
    }
    "api" {
        uvicorn api.main:app --host 0.0.0.0 --port 8000
    }
    "ui" {
        streamlit run ui/app.py --server.port=8501
    }
    "bootstrap-local" {
        python scripts/bootstrap_local.py
    }
    "archive" {
        python -m ops.archive --once
    }
    "retention" {
        python -m ops.retention --once
    }
    "parity" {
        python -m ops.parity --once
    }
    "backtest" {
        $featureSource = $env:FEATURE_SOURCE
        $args = @("--start", "2026-05-01T00:00:00Z", "--forward-hours", "24")
        if ($featureSource) { $args += @("--feature-source", $featureSource) }
        python -m backtest.runner @args
    }
    "forecast-ab" {
        python -m forecast.experiment
    }
    "refresh-rpc-pools" {
        python scripts/refresh_rpc_pools.py
    }
}
