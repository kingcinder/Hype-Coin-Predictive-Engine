# Sniper Scout Design

## Goal

Add a read-only module for investigating repeat early buyers, assessing early-buy
exposure on a token, and watching newly discovered launches as confirmed trades
arrive during the engine's normal scan cadence. Wallet scouting is the primary
view.

This module reports observed behavior and evidence coverage. A repeat early buyer
is a candidate, not a confirmed bot. Token lifecycle outcomes are market outcomes
for assets a wallet entered; they do not establish that wallet's realized profit
or return.

## Existing evidence

The ingestion pipeline writes `FirstBuy` rows from GeckoTerminal pool trades and,
on Solana when that feed fails, reconstructed pool transactions from RPC. Rows
include asset, wallet, observed trade time, seconds after pool creation, and an
optional USD size. `PairFirstWindowStats` distinguishes an observed empty window
from unavailable first-buy evidence. The current first-buy window is configurable.

The engine also emits `sniper_burst` ignition events and stores token lifecycle
events. The burst event is an activity signal, while wallet-level first buys are
the evidence for this scout. The existing Solana signature poller reads confirmed
transactions; it is not a pending-transaction or pre-confirmation mempool feed.

## Approaches considered

1. **Historical wallet report only.** Smallest change, but omits per-token context
   and active launch coverage.
2. **Provider-streaming launch monitor.** Could observe pending transactions on
   supported providers, but needs provider-specific subscriptions and has narrower
   chain coverage and additional traffic/operations requirements.
3. **Integrated scan-live scout (selected).** Compute wallet and token reports from
   existing first-buy records, and expose launches whose first-buy windows are
   still open. Existing ingestion continues collecting confirmed/indexed trades on
   each scan, without claiming pre-confirmation visibility.

## User-facing behavior

### Wallet scout

Rank wallets with repeat early entry across distinct assets. For each wallet show
its chain and address, number of distinct launches, median entry delay, number and
share of observed entries within the fast-entry threshold, and sample coverage.
Use the earliest recorded buy for each wallet/asset when calculating entry delay
and launch counts; later buys in the same asset do not count as extra launches.

The initial fast-entry threshold is 60 seconds and the minimum repeat-launch count
is two. Both values are configurable. The response includes the effective values
and sample counts so the ranking is interpretable. Do not call the result a bot
classification, success rate, or wallet profitability estimate.

Sort by fast-entry launch count descending, distinct launch count descending,
median entry delay ascending, then canonical wallet address for a stable tie-break.
Count a fast-entry launch once when that wallet's earliest observed buy for the
asset is within the threshold.

Where token lifecycle evidence exists, show the counts of entered assets with
observed collapse/rugged, survivor, or unresolved outcomes. Count each asset once
per wallet. An unresolved or absent outcome remains unknown and is not treated as
a survivor. Classify from the latest lifecycle event with both event time and
observation time at or before `as_of`; collapse/dead/rugged count as collapse,
survivor/parabolic/saturation count as survivor, and other or absent phases remain
unresolved.

### Token exposure

For one asset, report observed first-buy count, distinct early buyers, earliest and
median entry delay, the share of observed first buys inside the fast threshold,
top-one and top-five size shares among buyers with known USD sizes, and overlap
with wallets in the scout ranking. State the known-size denominator and when
USD sizes or first-buy feed coverage are incomplete.

### Scan-live launch watcher

List assets whose pools are within the configured first-buy window and show the
latest observed trade time, elapsed pool age, current early-buy counts, scout-wallet
overlap, and feed-coverage state. Refresh through the existing UI/API scan update
path. Evidence comes from confirmed/indexed GeckoTerminal trades and the existing
Solana RPC fallback. This view must label observations as scan-live confirmed trade
data; it must not imply pending transaction, MEV, or complete market coverage.

## Architecture and interfaces

- Add a focused `radar/snipers.py` module that builds bounded, chain-aware reports
  from `FirstBuy`, `PairFirstWindowStats`, assets/pools, and lifecycle evidence.
- Keep existing ingestion as the data collector. It already records first buys
  while a pool is inside the configured window; the watcher is a view over those
  persisted observations, not a second network poller.
- Add authenticated read endpoints:
  - `GET /snipers/wallets?chain=&days=30&limit=100&as_of=` for ranked repeat buyers.
  - `GET /snipers/active?chain=&limit=100&as_of=` for launches with an open
    first-buy window.
  - `GET /snipers/assets/{asset_id}?as_of=` for one token's early-buy exposure.
- Add a Sniper Scout UI view with the wallet list as its primary panel, active
  launches and selected-token exposure as secondary panels.
- Compute reports from existing records; do not add tables or a migration in the
  first version. Add `radar_sniper_fast_entry_seconds` (default 60) and
  `radar_sniper_min_repeat_launches` (default 2) to application configuration
  and its documented environment example.

## Time, coverage, and error rules

All report queries accept an optional `as_of` time (default: current UTC) and
exclude observations after it. Wallet aggregation is bounded by the requested
history window. Active launches
are limited to pools with known creation time and age within
`first_buy_window_minutes`; `PairFirstWindowStats.computed_at` supplies scan-time
freshness for their feed coverage.

Use the `PairFirstWindowStats` observation state to distinguish observed zero
activity from unavailable data. Missing entry sizes remain missing; size-based
concentration is reported with its known-size denominator. A missing chain feed,
unknown creation time, or unavailable provider result must not be presented as zero
sniper activity.

The module is read-only: it does not alter scoring, risk bands, copy signals, or
trading behavior. Existing `sniper_burst` events remain a separate activity signal.

## Verification

Add deterministic fixture tests without live RPC or provider calls for:

- minimum-launch filtering, chain filtering, history bounds, and deterministic
  wallet ranking;
- earliest wallet/asset entry handling, distinct launch counts, and fast-entry
  shares;
- observed token outcomes versus unresolved or missing outcomes;
- token buyer concentration with missing sizes and overlap with scout wallets;
- active launch window bounds, scan-time freshness, and unknown feed coverage;
- API schemas and UI handling of empty, partial, and populated reports.

Run the focused module/API/UI tests, migration-chain check (to confirm no schema
migration is introduced), lint, formatting, and type checks required by the repo.
