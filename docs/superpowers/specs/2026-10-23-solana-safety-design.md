# Solana Safety Evidence Design

## Goal

Record point-in-time Solana mint and supported pool safety evidence, expose it
to scoring without treating RPC failures as safe, and make the evidence
inspectable from the API and GUI.

## Data flow

The ingestion worker reads mint account data through the configured Solana RPC
pool and stores a timestamped JSON snapshot per asset. SPL and Token-2022
authority, supply, decimal, and extension fields are decoded into a stable
snapshot schema. LP classification is limited to parsed pool data that exposes
an LP mint and ownership evidence; unsupported layouts remain `unknown`.

The feature factory selects only snapshots with `observed_at <= decision_ts`,
then emits numeric risk features with missing flags when a fact is unknown or
there is no qualifying snapshot. The rule engine applies named point weights
to known active authorities and risk-bearing extensions. The existing token
detail feature snapshot exposes the resulting values; API detail also returns
the latest raw safety snapshot for inspection.

## Failure and history behavior

RPC errors, malformed responses, unsupported account layouts, and missing pool
ownership evidence stay missing/unknown. They never become a revoked,
disabled, or locked value. The collector records observation time, and the
feature factory rejects evidence observed after the decision timestamp.

## Verification

Recorded RPC fixtures cover revoked and live authorities, Token-2022 transfer
fees, timeout/missing semantics, LP unknown behavior, and future-observation
leakage. Migration, API serialization, rule weights, and GUI rendering receive
targeted checks; the full required project checks run before commit.
