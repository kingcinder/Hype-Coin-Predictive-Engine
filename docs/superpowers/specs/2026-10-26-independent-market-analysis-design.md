# Independent Market Analysis Design

## Goal

Add a searchable, evidence-based market analysis workflow that combines stored observations with a live DexScreener lookup, generates a separate LLM narrative from market evidence only, persists each analysis run, and surfaces contract addresses with GMGN links in token-focused UI areas.

## Current context

- The Streamlit UI has grouped sidebar navigation in `ui/app.py`; token score and detail flows already exist.
- The API uses FastAPI and SQLAlchemy models in `storage/models.py`. `/tokens/hot` and `/scores/top` return chain, address, and symbol. `TokenDetail` also returns the contract address.
- The database already stores timestamped `MarketSnapshot` and `LiquiditySnapshot` records connected to assets through pairs and pools.
- `ingestion.source_clients.DexScreenerClient` already provides search by query and token-pair lookup. Its normalizers and ingestion persistence path can be reused.
- The current LLM narrative routes consume engine features and/or scores. They are explicitly excluded from this workflow.
- Alembic migrations live under `storage/migrations/versions`; latest migration is `0023_risk_confidence_calibration`.

## User-approved requirements

1. Add a sidebar page for independent market analysis.
2. Search by token symbol, name, or contract address.
3. If a pasted CA is not stored, resolve it against DexScreener; when a valid market is found, save the token in the local asset universe for later search.
4. Show current live market data alongside available stored market/liquidity history. Include source and observation timestamps and make missing/stale data explicit.
5. Generate a separate LLM narrative whose inputs contain only the market report/evidence. Do not pass engine scores, forecasts, or engine-derived features.
6. Persist each analysis run with the generated narrative and the evidence snapshot used, with an analysis timestamp, so prior runs can be reviewed.
7. Surface CA text and a GMGN hyperlink icon in token-focused score lists and token detail views. Do not expand this to unrelated forecast/alert lists.
8. Preserve current score, ranking, and forecast behavior.

## Approaches considered

- **Broad search + live lookup + stored history + isolated narrative (selected):** supports arbitrary pasted CAs and known assets, can compare live and historical evidence, and saves auditable analyses. More implementation work because it needs an asset search API, provider resolution, a separate LLM call, and a new history table.
- **Database-only analysis:** smaller, but cannot analyze a new CA before prior ingestion.
- **Live listing only:** smaller, but provides no saved analysis or historical comparison.

## Architecture

### Search and asset resolution

Add an authenticated API endpoint for asset search. It searches the local universe by case-insensitive symbol/name/address prefix or substring, returns bounded results, and includes asset ID, chain, CA, symbol, and name. The UI presents these results as choices. A direct CA submission with no local match invokes DexScreener token-pair lookup; select the pair matching the submitted chain/address when possible. If multiple chains or market pairs remain ambiguous, show candidates and require the user to choose rather than silently analyze another contract.

For a resolved new token, reuse the existing normalized-pair and repository upsert path to persist its asset/contract/pair and initial live market/liquidity observations. Do not mark it scored or alter any score.

### Market report and isolated narrative

For the selected asset, assemble (a) a live DexScreener market snapshot and (b) recent stored price, volume, trades, and liquidity observations. Report source, timestamp, pair/venue, and freshness; show unavailable fields as missing. The independent narrative endpoint accepts/constructs a compact market-evidence payload and invokes the configured LLM provider through a dedicated method/prompt. The prompt instructs the model to describe evidence and uncertainty, not predict a guaranteed return. It must not read or serialize `Score`, `Forecast`, `Feature`, or engine narrative data.

Return a structured data report and narrative, then persist the exact input evidence snapshot, live source payload fields needed to reproduce the report, narrative text, model identifier, and generation timestamp in a new `market_analysis_runs` table keyed to the asset. A history endpoint lists past run timestamps and retrieves an individual saved result. Do not store API keys or full unfiltered provider responses.

### GMGN link and CA presentation

Add a centralized chain-to-GMGN URL helper for supported UI chains: Solana `sol`, Base `base`, and Ethereum `eth`. Render CA text in token detail and token-focused score tables, plus a small external-link icon using the GMGN URL. Keep normal browser-link behavior; links open through the user's browser and its configured external-link handling. Do not attempt to invoke the desktop's default browser from server-side Streamlit code.

### Navigation

Add an “Independent Market Analysis” view in the Intelligence section with a dedicated icon and dispatch branch. Use the existing grouped-radio navigation pattern.

## Data model and migration

Create `MarketAnalysisRun` with: integer primary key; asset foreign key; generated timestamp; LLM model identifier; JSON evidence snapshot; narrative text; optional risk/uncertainty narrative; and source metadata sufficient to identify the live provider and observation times. Add an index on `(asset_id, generated_at)`. Migration `0024_independent_market_analysis` creates and drops this table/index. The model and API schema must be included in normal Alembic metadata handling.

## Failure handling

- No local match and no live match: explain that no asset/market was found; do not save a fabricated token or analysis.
- Provider errors or rate limits: retain the searchable local result and stored-history report; show live data unavailable and do not imply freshness.
- LLM unavailable/disabled: show the assembled market report, explain narrative generation failed or is unavailable, and persist a report-only run only if the UI/API clearly labels its narrative status. Never substitute the existing score narrative.
- Missing stored observations: show “no stored history” and continue with live data.
- Ambiguous address/pair: require explicit candidate selection.
- Migration follows existing guarded/idempotent migration conventions.

## Validation

- Unit tests for chain-to-GMGN URL mapping, search matching/limits, DexScreener candidate selection, evidence assembly, and the independent prompt boundary (assert excluded score/forecast/feature fields are absent).
- API tests for local search, direct-CA live resolution, ambiguous/no-result cases, persisted run and history retrieval, and LLM failure behavior using mocked external boundaries.
- UI tests for navigation dispatch, search selection, CA/link rendering, data-only fallback, and history selection.
- Run targeted tests, then relevant full API/UI tests and static checks. Exercise the UI against the running local services if the environment permits, without restarting the engine worker.

## Out of scope

- Changing engine scoring, calibration, forecast models, or validation gates.
- Broad CA additions to alert, lifecycle, forecast, or other non-token-focused lists.
- Trading actions, wallet connections, or submitting transactions through GMGN.
- Guaranteeing that a GMGN page exists for every indexed token or that live provider values are complete.
