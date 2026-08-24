# Source Policy

The MVP uses a low-cost, public-first source hierarchy.

## Canonical and Strong Sources

1. Chain/RPC/event data for balances, logs, contract state, and finality-adjusted timestamps.
2. Venue-native or DEX market data for pair state, price, volume, and liquidity.
3. Verified official project, launch, audit, and exchange pages.
4. Explorer data for verification flags, ABI metadata, and transaction lookups.
5. Security scanner data as strong risk votes, never sole truth.

## Allowed MVP Sources

- DexScreener REST API
- GeckoTerminal public API
- Solana RPC
- Ethereum/Base RPC
- Etherscan V2 when `ETHERSCAN_API_KEY` is configured
- Public RSS/news feeds
- Static public project websites
- Public GitHub repository metadata

## Excluded Until Explicitly Reopened

- Auto-trading and exchange execution
- Custody or wallet signing
- Private GitHub ingestion
- Broad X/social scraping
- General browser automation across arbitrary sites
- Paid social intelligence vendors as required dependencies

## Missing Data

Missing or stale values must increase `UncertaintyScore` and reduce `ConfidenceScore`. They must not be treated as proof that a risk is absent.

