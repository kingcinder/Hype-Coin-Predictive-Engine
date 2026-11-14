from __future__ import annotations

from datetime import UTC, datetime

from trading.candidates import CANDIDATE_KEYS, build_candidates, get_candidate
from trading.price_sources import MarketSnapshotPriceSource

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_unknown_mint_is_none(session) -> None:
    source = MarketSnapshotPriceSource(session)
    assert get_candidate(session, "does-not-exist", now=NOW, price_source=source) is None


def test_candidate_has_exact_keys(session) -> None:
    from tests.conftest import seed_market_asset

    asset = seed_market_asset(session)
    session.commit()

    candidate = get_candidate(
        session,
        asset.address,
        now=NOW,
        price_source=MarketSnapshotPriceSource(session),
    )
    assert candidate is not None
    assert tuple(candidate) == CANDIDATE_KEYS
    assert candidate["mint"] == asset.address
    assert candidate["chain"] == "solana"
    assert candidate["missing"]  # no score yet
    assert candidate["evidence_ts"] is not None


def test_build_candidates_respects_n(session) -> None:
    from tests.conftest import seed_market_asset

    seed_market_asset(session)
    seed_market_asset(session, address="Token222222222222222222222222222222222222", symbol="TWO")
    session.commit()

    feed = build_candidates(session, n=1, now=NOW, price_source=MarketSnapshotPriceSource(session))
    assert set(feed) == {"candidates", "excluded_no_evidence", "generated_at"}
    assert len(feed["candidates"]) == 1
    assert tuple(feed["candidates"][0]) == CANDIDATE_KEYS
