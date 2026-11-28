# Holder-delta verification — mission-refactor Task 16, Step 1 (2026-11-28)

**Verdict: Branch A — per-wallet balance deltas are RECONSTRUCTIBLE
point-in-time from stored data, where holder-snapshot density and
wallet-set continuity suffice. No delta capture build is needed: the
capture already exists at ingest. Where coverage is insufficient the
distribution features read missing — never fabricated.**

## What is stored (category (b): not stored as deltas, not absent)

- `storage/models.py::Holder` stores `balance` per
  `(asset_id, wallet_address, ts, source_id)` — unique constraint
  `uq_holders_asset_wallet_ts` — plus `pct_supply` and an
  `observed_at` column, so every row carries both its snapshot time
  and its observation time for point-in-time gating. There is **no
  delta column**: a delta is the difference of one wallet's balances
  at two snapshot times, derived at read time.
- Both holder ingest paths write these rows per scan:
  `ingestion/service.py::_ingest_solana_holder_snapshots` (Solana RPC
  `getTokenLargestAccounts`) and `_ingest_evm_holder_snapshots`
  (GoPlus, Base/Ethereum) both call
  `storage/repository.py::insert_holder_once` once per account, with
  `ts = floor_to_hour(observed_at)` — an hourly grid. Repeated scans
  of the same asset therefore produce consecutive hourly per-wallet
  snapshots, which is exactly the series a delta derivation
  differences. (Branch B's premise — deltas absent from storage —
  does not hold: the raw material is already persisted, so a second
  capture table would duplicate it.)
- The factory already reads `Holder` point-in-time
  (`features/factory.py::_holder_features`: latest snapshot under
  `ts <= decision_ts AND observed_at <= decision_ts`, with a prior-ts
  comparison for growth). The Task 16 derivation
  (`_holder_balance_deltas`) follows the same gates.

## Density — the honest caveat

Reconstruction needs **two** things per decision point: at least two
snapshots at/before the decision, **and** the decision-time top-10
wallets present in the prior snapshot. Three structural facts bound
how often that holds live:

- Scan selection is narrow: `ingestion/service.py::
  _holder_scan_assets` takes the most recently `updated_at` assets
  per chain, limited by Settings `solana_holder_scan_limit = 1` and
  `evm_holder_scan_limit = 2` (per EVM chain) per scan cycle. Only
  the freshest few assets per chain accrue hourly holder series at
  any time; the broad episode population mostly has sparse or single
  snapshots.
- The Solana list is `getTokenLargestAccounts` (top ~20 accounts):
  the wallet set churns as balances reorder, so a wallet in the
  decision-time top 10 may have no prior-snapshot row even when the
  asset was scanned — an unobserved prior balance cannot be
  differenced, and entering the top list is not a measured flow.
- Discipline adopted in the derivation: `top_holder_net_flow` sums
  Δbalance only over top-10 wallets **with** a prior-snapshot row,
  and reads **missing** when fewer than half (5) of the top 10 are
  covered — a partial sum over a minority of the top holders would
  misread as a flow.

## Density queries (for the live-DB run)

Q1 — per-asset snapshot density over the episode population:

```sql
SELECT asset_id,
       COUNT(DISTINCT ts)        AS snapshot_count,
       COUNT(*)                  AS holder_rows,
       COUNT(DISTINCT wallet_address) AS distinct_wallets
FROM holders
GROUP BY asset_id;
```

Q2 — consecutive-pair coverage at each asset's latest snapshot
(share of the decision-time top 10 also present at the prior ts) is
implemented in the study probe: rank the latest snapshot's rows by
balance, take the top 10, count how many of those wallets appear at
the immediately preceding distinct `ts`.

**Execution proof (synthetic population, 2026-11-28):** both queries
were run against a scratch database seeded with three assets —
DENSE (5 hourly snapshots, stable 12-wallet set), SPARSE (1
snapshot), CHURN (3 snapshots, wallet set fully rotating):

```
Q1: DENSE  (5 distinct ts, 60 rows, 12 wallets)
    SPARSE (1 distinct ts, 12 rows, 12 wallets)
    CHURN  (3 distinct ts, 36 rows, 36 wallets)
Q2: DENSE  top-10 covered 10/10
    SPARSE no consecutive pair
    CHURN  top-10 covered 0/10
```

The CHURN row is the load-bearing one: snapshot *count* alone
overstates reconstructibility — continuity is the binding
constraint, and it is what the derivation's coverage rule measures.

**Live-population numbers are NOT in this note.** No copy of the
live DB exists on this VM (the same constraint recorded by Tasks 0,
5, 13 and 15), so Q1/Q2 over the real episode population remain a
deployment-step run. Per Task 0's rule, the ensemble is not graded
on live data until those numbers exist; the features ship with the
missing-data discipline above so sparse coverage degrades to
"missing", never to a fabricated flow.

## Branch taken

**Branch A.** Deliverables in this task: the delta derivation
(`features/factory.py::_holder_balance_deltas`), the three
distribution features (`effort_vs_result_depth_normalized`,
`top_holder_net_flow`, `holder_stall`), and the accelerate-vote
derivation (`features/factory.py::distribution_votes`) the exit
policy consumes. The `ExitPolicy`/`ExitSnapshot` wiring itself is
deferred: `trading/exit_policy.py` (Task 9) is not yet in the tree
and is owned by a concurrent work stream; the vote derivation is
built and tested now so the wiring is a mechanical follow-up.
