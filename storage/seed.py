from __future__ import annotations

from common.enums import SourceTier
from storage.database import SessionLocal
from storage.repository import get_or_create_chain, get_or_create_source, get_or_create_venue

CHAINS = [
    ("solana", "Solana", "solana", "SOL"),
    ("base", "Base", "evm", "ETH"),
    ("ethereum", "Ethereum", "evm", "ETH"),
]

SOURCES = [
    ("dexscreener", "market_data", SourceTier.VENUE.value, "https://api.dexscreener.com"),
    ("geckoterminal", "market_data", SourceTier.VENUE.value, "https://api.geckoterminal.com"),
    ("solana_rpc", "chain_rpc", SourceTier.CHAIN.value, "https://api.mainnet-beta.solana.com"),
    ("evm_rpc", "chain_rpc", SourceTier.CHAIN.value, None),
    ("etherscan_v2", "explorer", SourceTier.EXPLORER.value, "https://api.etherscan.io/v2/api"),
    ("rss_news", "news", SourceTier.PUBLIC_METADATA.value, None),
    ("website_probe", "website", SourceTier.OFFICIAL.value, None),
    ("github_public", "public_repo", SourceTier.PUBLIC_METADATA.value, "https://api.github.com"),
]


def seed_reference_data() -> None:
    with SessionLocal() as session:
        chain_rows = {}
        for slug, name, vm_type, native_symbol in CHAINS:
            chain_rows[slug] = get_or_create_chain(
                session, slug, name=name, vm_type=vm_type, native_symbol=native_symbol
            )
        for name, source_type, tier, base_url in SOURCES:
            get_or_create_source(
                session, name=name, source_type=source_type, tier=tier, base_url=base_url
            )
        for slug in ("solana", "base", "ethereum"):
            get_or_create_venue(
                session,
                name=f"{slug}_unknown_dex",
                venue_type="dex",
                chain_id=chain_rows[slug].id,
            )
        session.commit()


if __name__ == "__main__":
    seed_reference_data()
    print("Seeded chains, sources, and default venues.")
