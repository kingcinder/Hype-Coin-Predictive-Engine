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

## Policy-Gated Social Sources

The Nitter and X Trends crawlers remain in the codebase but are disabled by
default. Set `NIGHTCRAWLER_ENABLE_NITTER=1` or
`NIGHTCRAWLER_ENABLE_X_TRENDS=1` to explicitly opt in; startup emits a
one-time warning for each enabled source, and the crawler fleet labels disabled
sources as **Disabled by policy**. These unofficial feeds are fragile and do
not constitute ground truth. X Trends also reads Nitter RSS, so enabling it
does not make the feed an official X source.

## Excluded Until Explicitly Reopened

- Auto-trading and exchange execution
- Custody or wallet signing
- Private GitHub ingestion
- Broad X/social scraping
- General browser automation across arbitrary sites
- Paid social intelligence vendors as required dependencies

## Missing Data

Missing or stale values must increase `UncertaintyScore` and reduce `ConfidenceScore`. They must not be treated as proof that a risk is absent.
