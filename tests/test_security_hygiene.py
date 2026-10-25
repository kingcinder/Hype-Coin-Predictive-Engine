from __future__ import annotations

from crawlers.sources.nitter import parse_nitter_rss
from ui.live import SSE_TIMEOUT
from ui.theme import _CSS


def test_nitter_rss_rejects_entity_expansion() -> None:
    xml = """<!DOCTYPE rss [<!ENTITY leak "expanded">]>
    <rss><channel><item><title>&leak;</title></item></channel></rss>"""

    assert parse_nitter_rss(xml, "HYPE") == []


def test_live_sse_has_bounded_connect_and_read_timeouts() -> None:
    assert SSE_TIMEOUT.connect == 10.0
    assert SSE_TIMEOUT.write == 10.0
    assert SSE_TIMEOUT.pool == 10.0
    assert SSE_TIMEOUT.read == 90.0


def test_theme_substitution_keeps_css_literals_and_palette_values() -> None:
    assert "--sc-brand: #00ff88;" in _CSS
    assert "{{" not in _CSS and "}}" not in _CSS
