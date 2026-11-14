"""Provider keys become the primary RPC endpoint, with explicit precedence."""

from __future__ import annotations

from common.config import get_settings


def test_alchemy_key_is_the_ethereum_primary(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alchemy_api_key", "fake-key-for-tests")
    monkeypatch.setattr(settings, "quicknode_evm_rpc_url", None)
    monkeypatch.setattr(settings, "ethereum_rpc_url", "https://eth.example")

    assert (
        settings.rpc_url_for_chain("ethereum")
        == "https://eth-mainnet.g.alchemy.com/v2/fake-key-for-tests"
    )
    # Base is not enabled on this key, so its default stays in place.
    assert settings.rpc_url_for_chain("base") == "https://base.meowrpc.com"


def test_quicknode_overrides_alchemy(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alchemy_api_key", "fake-key-for-tests")
    monkeypatch.setattr(settings, "quicknode_evm_rpc_url", "https://quicknode.example")

    assert settings.rpc_url_for_chain("ethereum") == "https://quicknode.example"


def test_no_alchemy_key_falls_back_to_configured_url(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alchemy_api_key", None)
    monkeypatch.setattr(settings, "quicknode_evm_rpc_url", None)
    monkeypatch.setattr(settings, "ethereum_rpc_url", "https://eth.example")

    assert settings.rpc_url_for_chain("ethereum") == "https://eth.example"
