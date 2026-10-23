# Solana Safety Implementation Plan

> **For agentic workers:** Inline execution under the authorized T3 goal. Each section ends with a targeted check before the next section.

**Goal:** Add point-in-time Solana mint and supported LP safety evidence to ingestion, scoring, API, and GUI.

**Architecture:** A pooled-RPC collector stores normalized JSON snapshots in a dedicated table. The feature factory selects only observations no later than `decision_ts`; risk rules score known dangerous facts and preserve missing values as missing.

**Tech Stack:** Python 3.12, SQLAlchemy/Alembic, existing `HttpClient` and `RpcEndpointPool`, FastAPI, Streamlit, pytest.

## Global Constraints

- Keep the engine research-only; do not add trading, wallet, or custody behavior.
- Enforce `observed_at <= decision_ts` through `validation/leakage_guard.py`.
- RPC errors and unsupported layouts must remain missing or unknown.
- Tests use recorded fixtures and must not access live RPC endpoints.

### Task 1: Define decoder and persistence behavior

**Files:** Create `tests/test_solana_safety.py`; create `ingestion/solana_safety.py`; create an Alembic migration; modify `storage/models.py`.

- [ ] Add failing fixture tests for authority states, Token-2022 transfer fee, LP unknown, and RPC timeout.
- [ ] Add a failing migration/model test for timestamped JSON snapshot persistence.
- [ ] Implement typed decode results, pooled request handling, and snapshot persistence.
- [ ] Run the targeted Solana safety tests.

### Task 2: Enforce point-in-time feature and risk behavior

**Files:** Modify `features/definitions.py`, `features/factory.py`, `risk_engine/rules.py`; extend `tests/test_solana_safety.py`.

- [ ] Add failing tests for missing semantics, known risk point weights, and a future snapshot rejected by `decision_ts`.
- [ ] Implement snapshot lookup and feature emission through the leakage guard.
- [ ] Add documented named rule weights and missing-feature propagation.
- [ ] Run targeted feature and risk tests.

### Task 3: Wire ingestion, API, and GUI

**Files:** Modify `ingestion/service.py`, `api/main.py`, `api/schemas.py`, `ui/app.py`; add docs.

- [ ] Add failing checks for the token detail API snapshot and GUI safety rendering.
- [ ] Collect safety snapshots in the normal Solana ingestion cycle using configured mints.
- [ ] Return latest observed snapshot in token detail and render known/unknown facts in the GUI.
- [ ] Run targeted API and GUI checks, then all five required repository checks.
- [ ] Update `STATE.md` N1 evidence and commit T3.
