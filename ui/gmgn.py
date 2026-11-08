from __future__ import annotations

from html import escape
from urllib.parse import quote

_GMGN_CHAINS = {"solana": "sol", "base": "base", "ethereum": "eth"}


def gmgn_token_url(chain: str, address: str) -> str | None:
    slug = _GMGN_CHAINS.get(chain.lower())
    return f"https://gmgn.ai/{slug}/token/{quote(address, safe='')}" if slug and address else None


def gmgn_link_html(chain: str, address: str) -> str:
    url = gmgn_token_url(chain, address)
    if not url:
        return escape(address)
    return (
        f"<code>{escape(address)}</code> "
        f'<a href="{escape(url, quote=True)}" target="_blank" '
        'rel="noopener noreferrer" title="Open token on GMGN">↗</a>'
    )
