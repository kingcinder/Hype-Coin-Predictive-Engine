# Independent Market Analysis Implementation Plan

> Inline execution in the canonical checkout; preserve existing unrelated changes.

**Goal:** Add searchable, saved independent market analyses combining live DexScreener data and stored observations, plus CA/GMGN links in token-focused UI.

**Architecture:** Add bounded asset search and analysis/history endpoints to FastAPI, a `MarketAnalysisRun` model and guarded Alembic migration, and a dedicated market-evidence LLM prompt that never loads scores, features, or forecasts. Add Streamlit search/report/history UI and reusable GMGN URL/CA display helpers.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Alembic, Streamlit, httpx, pytest.

## Global Constraints

- Preserve existing score, ranking, and forecast behavior.
- Search new CAs using DexScreener and save resolved market assets without scoring them.
- Keep independent narrative inputs limited to live and stored market evidence.
- Preserve source timestamps, evidence snapshots, and analysis history.
- Add CA/GMGN links only to token-focused score/detail UI.
- Keep links as ordinary browser links; do not attempt server-side desktop-browser invocation.
- TDD: write targeted failing tests, observe failure, implement, then verify.
- Do not alter or discard the existing dirty `ui/app.py`, `ui/health_diagnostics_view.py`, or `tests/test_ui_navigation.py` changes.

---

## File map

- `storage/models.py`: analysis-history ORM table.
- `storage/migrations/versions/0024_independent_market_analysis.py`: guarded schema migration.
- `ingestion/market_analysis.py`: DexScreener resolution and market evidence assembly.
- `llm/engine.py`: isolated market-evidence narrative method and response parsing.
- `api/schemas.py`, `api/main.py`: search, analyze, and history API contracts/routes.
- `ui/app.py`: sidebar entry, search/report/history view, CA/GMGN link rendering, score tables/detail.
- `tests/test_market_analysis.py`, `tests/test_api.py`, and focused UI tests: resolver, evidence, model isolation, persistence, navigation and rendering.

### Task 1: Build pure market-evidence and GMGN helpers

**Interfaces:**

- `gmgn_token_url(chain: str, address: str) -> str | None`.
- `select_dexscreener_pair(pairs: list[dict[str, Any]], address: str, chain: str | None) -> dict[str, Any] | None`.
- `build_market_evidence(live_pair: dict[str, Any] | None, stored_rows: ...) -> dict[str, Any]` with source timestamps and explicit missing values.

- [ ] Add focused unit tests for supported/unsupported GMGN chains, case-insensitive CA and chain matching, missing live fields, and evidence timestamps.
- [ ] Run those tests and verify they fail because helpers do not exist.
- [ ] Implement the helpers in a focused module and add the reusable UI URL helper where the UI can import it.
- [ ] Run the focused tests.

### Task 2: Persist analysis runs and provide APIs

**Interfaces:**

- `GET /assets/search?q=<query>&limit=...` returns bounded asset results with `asset_id`, `chain`, `address`, `symbol`, and `name`.
- `POST /market-analysis/{asset_id}` refreshes DexScreener market data, assembles report + stored history, generates independent narrative, saves one run, and returns it.
- `GET /market-analysis/{asset_id}/history` returns saved run summaries newest first.
- `GET /market-analysis/runs/{run_id}` returns a saved evidence snapshot and narrative.

- [ ] Add API tests for local symbol/name/CA search, result limits, new CA resolution, ambiguity/no-result handling, report-only persistence on LLM unavailable, and history retrieval.
- [ ] Run tests and verify expected failures.
- [ ] Add ORM model and migration `0024_independent_market_analysis` with index `(asset_id, generated_at)`.
- [ ] Implement DexScreener lookups and historical query using existing clients/models; upsert newly resolved assets/pairs and initial live observations without invoking scoring.
- [ ] Add an isolated LLM method taking only market evidence. Add endpoint orchestration and persist the exact prompt evidence, status, model identifier, narrative, and timestamp.
- [ ] Run API/unit tests plus migration/schema tests.

### Task 3: Add Streamlit analysis page and token CA links

- [ ] Add UI tests for sidebar dispatch, CA/GGMN links, analysis search, failed-live-data fallback, narrative unavailable state, and selecting a saved history record.
- [ ] Run them to verify expected failures.
- [ ] Add “Independent Market Analysis” under Intelligence and a form accepting symbol/name/CA search; require an explicit candidate choice when multiple results exist.
- [ ] Render current market report, stored history, source timestamps/freshness, independent narrative status, and saved prior runs.
- [ ] Add CA and GMGN external-link icon to `score_table`, token detail, and token-focused selector/table views. Preserve existing view columns and score semantics.
- [ ] Run focused UI tests and verify interactions in the existing Streamlit session if available.

### Task 4: Verify integration

- [ ] Run focused test modules and relevant API, migration, and UI navigation suites.
- [ ] Run lint/type or syntax checks used by the repository.
- [ ] Check `git diff` and status to verify unrelated dirty edits remain intact and no secrets or database files changed.
- [ ] Exercise the new page against the existing local UI/API processes if available; do not start the engine worker.

## Self-review

- Every spec requirement maps to Tasks 1–4: search and resolution, live/stored report, isolated narrative, saved history/migration, navigation, token CAs/GMGN links, errors, and verification.
- Data access remains separated from score/forecast logic; the LLM method accepts only the evidence object.
- External network, LLM, and persistence boundaries are mocked in tests; the real UI verification is an integration check only.
