from ui.app import NAV_SECTIONS
from ui.gmgn import gmgn_link_html, gmgn_token_url


def test_analysis_page_is_in_sidebar_navigation() -> None:
    intelligence = dict(NAV_SECTIONS)["Intelligence"]
    assert "Independent Market Analysis" in intelligence


def test_gmgn_link_escapes_contract_address_and_opens_new_tab() -> None:
    link = gmgn_link_html("base", "<script>alert(1)</script>")
    assert "<script>" not in link
    assert "&lt;script&gt;" in link
    assert 'target="_blank"' in link
    assert gmgn_token_url("unsupported", "CA") is None
