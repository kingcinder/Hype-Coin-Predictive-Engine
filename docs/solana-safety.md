# Solana safety evidence

The ingestion worker stores a timestamped snapshot of SPL/Token-2022 mint
metadata for the bounded Solana holder scan cohort. The snapshot includes
mint/freeze authority state, raw supply, decimals, Token-2022 transfer-fee,
permanent-delegate, non-transferable, and default-account-state extensions.
The `/tokens/{id}` response includes the latest snapshot; token detail also
shows the evidence timestamp and unknown states.

Feature builds select the newest snapshot whose `observed_at` is at or before
`decision_ts`. No qualifying snapshot, an RPC timeout, or an undecodable field
is missing and reduces feature coverage; it never means the control is
revoked or disabled.

## Risk points

The rule engine adds points only for confirmed active controls:

| Evidence | Points |
| --- | ---: |
| Mint authority active | 20 |
| Freeze authority active | 25 |
| Token-2022 transfer fee | 12 |
| Token-2022 permanent delegate | 20 |
| Token-2022 non-transferable | 10 |
| Default account state frozen | 12 |
| LP tokens known to be unlocked | 18 |

These are named heuristic weights, not calibrated probabilities. Unknown
evidence adds no points and remains listed as missing.

## LP limits

Pool addresses come from the Solana pairs already ingested by the engine. LP
status is classified only when a provider's parsed pool account includes an LP
mint, full LP supply, and account ownership sufficient to establish unlocked,
locked, or burned supply. Other DEX layouts remain `unknown`; the collector does
not infer lock status from liquidity depth or pool age.

Apply database migrations with the normal Alembic upgrade path before running
an updated worker. Safety collection uses the configured Solana RPC pool and
the existing `solana_holder_scan_limit` and RPC pause settings.
