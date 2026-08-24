from __future__ import annotations

import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from common.time import utc_now
from ingestion.rpc_pool import (
    HEALTH_START,
    PROBE_EVERY_PICKS,
    RpcEndpointPool,
    get_rpc_pool,
    persist_pool_snapshots,
)
from ingestion.source_clients import SolanaRpcClient

POOL = ["https://rpc-a.example.com", "https://rpc-b.example.com", "https://rpc-c.example.com"]


def test_pick_prefers_healthy_and_round_robins_on_tie() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=2)
    assert pool.pick() == POOL[0]
    assert pool.pick() == POOL[1]
    assert pool.pick() == POOL[2]
    # LRU tiebreak cycles back to the first endpoint.
    assert pool.pick() == POOL[0]


def test_failure_decays_health_and_fails_over() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=2)
    first = pool.pick()
    assert first == POOL[0]
    pool.mark_failure(first)
    # Health of A dropped below the others, so failover to B.
    assert pool.pick() == POOL[1]
    assert pool.health(first) < HEALTH_START


def test_endpoint_goes_down_after_threshold_and_is_excluded() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=2)
    for _ in range(2):
        pool.mark_failure(POOL[0])
    snapshot = {state.url: state for state in pool.snapshot()}
    assert snapshot[POOL[0]].down is True
    # Down endpoint is skipped; picks come from the healthy set.
    assert pool.pick() == POOL[1]
    assert pool.pick() == POOL[2]
    assert POOL[0] not in {pool.pick() for _ in range(5)}


def test_down_endpoint_is_probed_and_recovers_on_success() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=1)
    pool.mark_failure(POOL[0])
    assert pool.snapshot()[0].down is True
    # Healthy picks keep going to B/C until the probe slot arrives.
    probed = None
    for _ in range(PROBE_EVERY_PICKS):
        picked = pool.pick()
        if picked == POOL[0]:
            probed = picked
            break
    assert probed == POOL[0]
    pool.mark_success(probed)
    state = pool.snapshot()[0]
    assert state.down is False
    assert state.health >= pool.recovery_health


def test_all_down_picks_least_recently_used() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=1)
    for url in POOL:
        pool.mark_failure(url)
    assert all(state.down for state in pool.snapshot())
    # Nothing healthy left: the pool keeps trying the least-recently-used.
    picked = pool.pick()
    assert picked in POOL
    pool.mark_success(picked)
    assert pool.health(picked) >= pool.recovery_health


def test_zero_healthy_alert_is_deduplicated_until_recovery() -> None:
    pool = RpcEndpointPool(POOL[:2], failure_threshold=1, chain_slug="base")
    for url in POOL[:2]:
        pool.mark_failure(url)

    events = []

    def callback(event):
        events.append(event)
        return True

    assert pool.dispatch_alerts(callback, cooldown_seconds=900) == 1
    assert events[0].kind == "zero_healthy_endpoints"
    assert events[0].chain_slug == "base"
    assert pool.dispatch_alerts(callback, cooldown_seconds=900) == 0

    pool.mark_success(POOL[0])
    assert pool.dispatch_alerts(callback, cooldown_seconds=900) == 0
    pool.mark_failure(POOL[0])
    assert pool.dispatch_alerts(callback, cooldown_seconds=900) == 1
    assert events[-1].kind == "zero_healthy_endpoints"


def test_endpoint_down_alert_waits_for_cooldown_and_retries_failed_delivery() -> None:
    pool = RpcEndpointPool(POOL[:2], failure_threshold=1, chain_slug="ethereum")
    pool.mark_failure(POOL[0])
    now = utc_now()
    events = []
    assert pool.dispatch_alerts(
        lambda event: events.append(event) or True,
        cooldown_seconds=900,
        now=now,
    ) == 0
    assert pool.dispatch_alerts(
        lambda _event: False,
        cooldown_seconds=900,
        now=now + timedelta(seconds=901),
    ) == 0
    assert pool.dispatch_alerts(
        lambda event: events.append(event) or True,
        cooldown_seconds=900,
        now=now + timedelta(seconds=902),
    ) == 1
    assert events[-1].kind == "endpoint_down_cooldown"
    assert events[-1].url == POOL[0]


def test_success_boost_accumulates_without_exceeding_one() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=2)
    for _ in range(50):
        pool.mark_success(POOL[0])
    assert pool.health(POOL[0]) == 1.0


def test_empty_pool_raises() -> None:
    with pytest.raises(RuntimeError):
        RpcEndpointPool([]).pick()


def test_solana_client_picks_from_pool_and_marks_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "ingestion.source_clients.get_rpc_pool",
        lambda chain_slug: RpcEndpointPool(POOL, failure_threshold=2),
    )
    client = SolanaRpcClient(pool_enabled=True)
    assert client.http.base_url in POOL
    assert client._pool is not None

    def boom(*_args, **_kwargs):
        raise ConnectionError("endpoint down")

    monkeypatch.setattr(client.http, "post_json", boom)
    with pytest.raises(ConnectionError):
        client.rpc("getHealth")
    assert client._pool.health(client.http.base_url) < HEALTH_START


def test_solana_client_fallback_without_pool(monkeypatch) -> None:
    monkeypatch.setattr(
        "ingestion.source_clients.get_rpc_pool",
        lambda chain_slug: RpcEndpointPool(POOL, failure_threshold=2),
    )
    client = SolanaRpcClient(pool_enabled=False)
    assert client._pool is None
    assert "api.mainnet-beta.solana.com" in client.http.base_url


def test_get_rpc_pool_is_cached_singleton_per_chain() -> None:
    get_rpc_pool.cache_clear()
    try:
        first = get_rpc_pool("solana")
        second = get_rpc_pool("solana")
        base = get_rpc_pool("base")
        eth = get_rpc_pool("ethereum")
        assert first is second
        assert first is not base
        assert base is not eth
        assert first.enabled
        assert base.enabled
        assert eth.enabled
        assert first.endpoints[0] != base.endpoints[0]
    finally:
        get_rpc_pool.cache_clear()


def test_background_probe_recovers_down_endpoint_without_picks() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=1)
    pool.mark_failure(POOL[0])
    assert pool.snapshot()[0].down is True

    pool.start_background_probe(
        lambda url: url == POOL[0],
        interval_seconds=0.05,
    )
    try:
        # Recovery must happen on the timer alone — no pick() calls at all.
        deadline = 3.0
        elapsed = 0.0
        while elapsed < deadline and pool.snapshot()[0].down:
            time.sleep(0.05)
            elapsed += 0.05
        state = pool.snapshot()[0]
        assert state.down is False
        assert state.health >= pool.recovery_health
    finally:
        pool.stop_background_probe()


def test_background_probe_failure_keeps_endpoint_down() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=1)
    pool.mark_failure(POOL[0])
    pool.start_background_probe(lambda _url: False, interval_seconds=0.05)
    try:
        time.sleep(0.15)
        assert pool.snapshot()[0].down is True
        assert pool.snapshot()[0].consecutive_failures > 1
    finally:
        pool.stop_background_probe()


def test_start_background_probe_is_idempotent() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=1)
    assert pool.start_background_probe(lambda _url: True, interval_seconds=0.05) is True
    try:
        assert pool.start_background_probe(lambda _url: True, interval_seconds=0.05) is True
        assert pool._background_thread is not None
        assert pool._background_thread.is_alive()
        # While the background probe is running, the pick-probe slot is off.
        pool.mark_failure(POOL[0])
        for _ in range(PROBE_EVERY_PICKS * 2):
            assert pool.pick() != POOL[0]
    finally:
        pool.stop_background_probe()


def test_stop_background_probe_restores_pick_probe_slot() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=1)
    pool.mark_failure(POOL[0])
    pool.start_background_probe(lambda _url: False, interval_seconds=0.05)
    pool.stop_background_probe()
    assert pool._background_thread is None or not pool._background_thread.is_alive()
    probed = None
    for _ in range(PROBE_EVERY_PICKS):
        picked = pool.pick()
        if picked == POOL[0]:
            probed = picked
            break
    assert probed == POOL[0]


def test_probe_down_endpoints_synchronous() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=1)
    pool.mark_failure(POOL[0])
    pool.mark_failure(POOL[1])

    def probe(url: str) -> bool:
        return url == POOL[0]

    assert pool.probe_down_endpoints(probe) == 2
    recovered = pool.snapshot()[0]
    assert recovered.down is False
    assert recovered.health >= pool.recovery_health
    assert recovered.probe_count == 1
    assert recovered.probe_successes == 1
    assert recovered.probe_failures == 0
    assert recovered.last_probe_ok is True
    assert len(recovered.probe_history) == 1
    assert pool.snapshot()[1].down is True


def test_probe_down_endpoints_never_raises() -> None:
    pool = RpcEndpointPool(POOL, failure_threshold=1)
    pool.mark_failure(POOL[0])

    def boom(_url: str) -> bool:
        raise ConnectionError("probe failed hard")

    assert pool.probe_down_endpoints(boom) == 1
    assert pool.snapshot()[0].down is True


def test_ensure_background_probe_starts_per_chain_and_respects_config(monkeypatch) -> None:
    from common.config import get_settings
    from ingestion import source_clients

    settings = get_settings()
    original_enabled = settings.rpc_pool_background_probe_enabled
    original_pool_enabled = settings.rpc_pool_enabled
    try:
        settings.rpc_pool_background_probe_enabled = True
        settings.rpc_pool_enabled = True
        pools = {
            "solana": RpcEndpointPool(["https://rpc-sol.example.com"], failure_threshold=1),
            "base": RpcEndpointPool(["https://rpc-base.example.com"], failure_threshold=1),
            "ethereum": RpcEndpointPool(["https://rpc-eth.example.com"], failure_threshold=1),
        }
        monkeypatch.setattr(source_clients, "get_rpc_pool", lambda chain_slug: pools[chain_slug])
        assert source_clients.ensure_background_probe() is True
        for pool in pools.values():
            assert pool._background_thread is not None
            assert pool._background_thread.is_alive()
        for pool in pools.values():
            pool.stop_background_probe()

        settings.rpc_pool_background_probe_enabled = False
        assert source_clients.ensure_background_probe() is False
        for pool in pools.values():
            assert pool._background_thread is None or not pool._background_thread.is_alive()
    finally:
        settings.rpc_pool_background_probe_enabled = original_enabled
        settings.rpc_pool_enabled = original_pool_enabled


def test_get_rpc_url_rotates_base_pool(monkeypatch) -> None:
    from ingestion import source_clients

    pools = {
        "base": RpcEndpointPool(["https://rpc-base.example.com"], failure_threshold=1),
        "ethereum": RpcEndpointPool(["https://rpc-eth.example.com"], failure_threshold=1),
    }
    monkeypatch.setattr(source_clients, "get_rpc_pool", lambda chain_slug: pools[chain_slug])
    assert source_clients.get_rpc_url("base") == "https://rpc-base.example.com"
    assert source_clients.get_rpc_url("ethereum") == "https://rpc-eth.example.com"


def test_persist_pool_snapshots_writes_endpoint_state_and_history(session, monkeypatch) -> None:
    from ingestion import rpc_pool as rpc_pool_module
    from storage import models

    pools = {
        chain: RpcEndpointPool([f"https://{chain}.example.com"], failure_threshold=1)
        for chain in ("solana", "base", "ethereum")
    }
    pools["base"].mark_failure("https://base.example.com")
    monkeypatch.setattr(rpc_pool_module, "get_rpc_pool", lambda chain: pools[chain])

    assert persist_pool_snapshots(session) == 3
    session.flush()
    row = session.scalar(
        select(models.RpcPoolSnapshot).where(models.RpcPoolSnapshot.chain_slug == "base")
    )
    assert row is not None
    assert row.down is True
    assert row.health < 1.0
    assert row.probe_count == 0
    assert row.probe_history == []


def test_record_pool_health_writes_per_chain_rows(session, monkeypatch) -> None:
    from common.config import get_settings
    from ingestion import rpc_pool as rpc_pool_module
    from storage import models

    settings = get_settings()
    original_enabled = settings.rpc_pool_enabled
    try:
        settings.rpc_pool_enabled = True
        pools = {
            "solana": RpcEndpointPool(["https://rpc-sol.example.com"], failure_threshold=1),
            "base": RpcEndpointPool(["https://rpc-base.example.com"], failure_threshold=1),
            "ethereum": RpcEndpointPool(["https://rpc-eth.example.com"], failure_threshold=1),
        }
        pools["solana"].mark_failure("https://rpc-sol.example.com")
        pools["base"].mark_failure("https://rpc-base.example.com")
        monkeypatch.setattr(
            rpc_pool_module, "get_rpc_pool", lambda chain_slug: pools[chain_slug]
        )

        rpc_pool_module.record_pool_health(session)
        session.flush()

        states: dict[str, str] = {}
        for component in ("rpc_pool:solana", "rpc_pool:base", "rpc_pool:ethereum"):
            row = session.scalar(
                select(models.SystemHealth).where(models.SystemHealth.component == component)
            )
            assert row is not None, component
            states[component] = row.state
        assert states["rpc_pool:solana"] == "red"
        assert states["rpc_pool:base"] == "red"
        assert states["rpc_pool:ethereum"] == "ok"
    finally:
        settings.rpc_pool_enabled = original_enabled


def test_probe_rpc_endpoint_chain_dispatch(monkeypatch) -> None:
    from ingestion import source_clients

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            return FakeResponse(self._payload)

    def fake_client_factory(*_args, **_kwargs):
        return FakeClient({"result": "ok"})

    sent: list[dict] = []

    class RecordingClient(FakeClient):
        def post(self, *args, **kwargs):
            sent.append(kwargs["json"])
            return FakeResponse(self._payload)

    def recording_factory(*_args, **_kwargs):
        return RecordingClient({"result": "ok"})

    monkeypatch.setattr(source_clients.httpx, "Client", recording_factory)
    assert source_clients.probe_rpc_endpoint("solana", "https://rpc.example.com") is True
    assert sent[-1]["method"] == "getHealth"
    assert source_clients.probe_rpc_endpoint("base", "https://rpc.example.com") is True
    assert sent[-1]["method"] == "eth_blockNumber"

    def failing_factory(*_args, **_kwargs):
        return FakeClient({"error": {"code": -32005}})

    monkeypatch.setattr(source_clients.httpx, "Client", failing_factory)
    assert source_clients.probe_rpc_endpoint("ethereum", "https://rpc.example.com") is False
    # Unknown chains must not accidentally receive an EVM probe.
    assert source_clients.probe_rpc_endpoint("polygon", "https://rpc.example.com") is False
