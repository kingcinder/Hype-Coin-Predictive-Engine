"""Own-session-cycle tests for the migration scripts' injectable-session seams.

``scripts/rescore.py`` established the pattern: an optional ``session=None``
keyword lets tests drive the logic against an injected session while the CLI
path (``__main__``) opens and owns its own ``session_scope()`` cycle. This
file applies the audit to the other one-off migration scripts:

- ``scripts/backfill_history.py`` — ``backfill_coingecko`` /
  ``backfill_defillama`` now take ``*, session=None`` and route through
  ``_*_in_session`` bodies that never close a caller's session; the CLI
  ``main()`` deliberately passes NO session.
- ``scripts/seed_fixtures.py`` — ``seed_fixture_data(*, session=None)`` and a
  ``_seed_in_session`` body; schema creation is bound to the active session.

Each own-cycle test swaps ``scripts.<module>.session_scope`` for the shared
``TrackingScope`` (tests/conftest.py) and asserts the cycle is entered exactly
once and closed on exit — the same contract ``rescore`` is pinned to — plus
behavioral checks that dry-runs persist nothing and injected seeds land.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from storage import models
from storage.database import Base
from storage.repository import get_or_create_source, store_raw_evidence
from tests.conftest import TrackingScope, seed_market_asset

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mem_session() -> tuple[Session, object]:
    """Fresh in-memory DB (StaticPool → one shared connection) + open session."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    ses = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return ses(), engine


def _disable_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the scoring ensemble's LLM layer off, whatever the settings cache.

    Patching exactly ``scoring.engine.get_settings`` is sufficient by design:
    ``score_current_assets`` builds a fresh ``ScoringEngine`` per call, so
    ``self.settings = get_settings()`` is captured AFTER this patch, and the
    ``if self.settings.llm_enabled and scores`` gate short-circuits the whole
    LLM pass before ``llm.engine``'s singleton is even imported. Neither
    ``scoring.llm_ensemble`` (``apply_llm_adjustments`` is pure) nor the
    llm_calibrator touches the network behind a fresh engine, so no other
    module read of the setting exists to patch.
    """
    from common.config import get_settings

    real = get_settings()
    monkeypatch.setattr(
        "scoring.engine.get_settings",
        lambda: real.model_copy(update={"llm_enabled": False}),
    )


def _seed_coingecko_evidence(session: Session, *, symbol: str = "HYPE") -> None:
    """Coingecko evidence row so _resolve_coingecko_ids needs no network."""
    source = get_or_create_source(
        session,
        name="coingecko",
        source_type="market_data",
        tier="public_metadata",
        base_url="https://api.coingecko.com/api/v3",
    )
    store_raw_evidence(
        session,
        source=source,
        payload={"items": [{"symbol": symbol, "metrics": {"coingecko_id": "hype-coin"}}]},
        observed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.commit()


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.backfill_history.time.sleep", lambda _seconds: None)


def _snapshot_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(models.MarketSnapshot)))


# ---------------------------------------------------------------------------
# backfill_history.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "fn_name", "canned_rows", "inserted_expected"),
    [
        pytest.param(
            "coingecko",
            "backfill_coingecko",
            [(datetime(2026, 8, 30, tzinfo=UTC), 1.5), (datetime(2026, 8, 31, tzinfo=UTC), 1.6)],
            2,
            id="coingecko",
        ),
        pytest.param(
            "defillama",
            "backfill_defillama",
            None,  # patched per-ref below
            1,
            id="defillama",
        ),
    ],
)
def test_backfill_session_none_uses_own_cycle(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    fn_name: str,
    canned_rows: list[tuple[datetime, float]] | None,
    inserted_expected: int,
) -> None:
    """session=None (the CLI shape) opens/owns one session_scope() cycle and
    dry-run persists nothing on the override DB."""
    import scripts.backfill_history as bh

    target, engine = _mem_session()
    seed_market_asset(target)  # 1 asset + pair, 3 snapshots, committed
    if provider == "coingecko":
        _seed_coingecko_evidence(target)
        monkeypatch.setattr(
            "scripts.backfill_history._coingecko_history", lambda client, coin_id, days: canned_rows
        )
    else:
        monkeypatch.setattr(
            "scripts.backfill_history._defillama_history",
            lambda client, refs, days: {
                ref: [(datetime(2026, 8, 30, tzinfo=UTC), 1.5)] for ref in refs
            },
        )
    _no_sleep(monkeypatch)

    before = _snapshot_count(target)
    scope = TrackingScope(target)
    monkeypatch.setattr("scripts.backfill_history.session_scope", lambda: scope)

    fn = getattr(bh, fn_name)
    result = fn(days=7, dry_run=True)

    assert scope.entered == 1
    assert scope.closed is True
    assert result["dry_run"] is True
    assert result["assets_with_pairs"] == 1
    assert result["snapshots_inserted"] == inserted_expected

    # dry-run wrote nothing: count via a fresh connection on the same engine.
    check = sessionmaker(bind=engine)()
    try:
        assert _snapshot_count(check) == before
    finally:
        check.close()


def test_backfill_main_cli_path_uses_own_cycle(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """main() must NOT pass a session — the injectable seam is unreachable
    from argv, so the real argparse → session_scope() → worker path works."""
    import scripts.backfill_history as bh

    target, _engine = _mem_session()
    seed_market_asset(target)
    _seed_coingecko_evidence(target)
    monkeypatch.setattr(
        "scripts.backfill_history._coingecko_history",
        lambda client, coin_id, days: [(datetime(2026, 8, 30, tzinfo=UTC), 1.5)],
    )
    _no_sleep(monkeypatch)

    scope = TrackingScope(target)
    monkeypatch.setattr("scripts.backfill_history.session_scope", lambda: scope)

    rc = bh.main(["--provider", "coingecko", "--days", "7", "--dry-run"])

    assert rc == 0
    assert scope.entered == 1, "main() must use its own session cycle, not an injected one"
    assert scope.closed is True
    out = capsys.readouterr().out
    assert "'dry_run': True" in out, out


# ---------------------------------------------------------------------------
# seed_fixtures.py
# ---------------------------------------------------------------------------


def test_seed_fixture_data_session_none_uses_own_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """seed_fixture_data() (session=None, the CLI path) owns one cycle and the
    seeded rows land in the override DB."""
    import scripts.seed_fixtures as sf

    _disable_llm(monkeypatch)
    target, engine = _mem_session()
    scope = TrackingScope(target)
    monkeypatch.setattr("scripts.seed_fixtures.session_scope", lambda: scope)

    sf.seed_fixture_data()

    assert scope.entered == 1
    assert scope.closed is True

    check = sessionmaker(bind=engine)()
    try:
        symbols = {row for row in check.scalars(select(models.Asset.symbol)).all()}
        assert {"HYPE", "DANGER", "USDC"} <= symbols
        assert int(check.scalar(select(func.count()).select_from(models.Holder))) == 2
        assert int(check.scalar(select(func.count()).select_from(models.MarketSnapshot))) == 6
        flag = check.scalar(
            select(models.ContractFlag).where(
                models.ContractFlag.flag_type == "mint_or_freeze_danger"
            )
        )
        assert flag is not None and flag.severity == "critical"
        assert int(check.scalar(select(func.count()).select_from(models.Score))) >= 1
    finally:
        check.close()


def test_seed_fixture_data_injected_session_seeds_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit session= lets the caller own the lifecycle: the injected
    session survives the call and sees every seeded row + schema table."""
    import scripts.seed_fixtures as sf

    _disable_llm(monkeypatch)
    session, _engine = _mem_session()

    sf.seed_fixture_data(session=session)

    # Caller-owned: the session is still open and usable after the call.
    session.expire_all()
    symbols = {row for row in session.scalars(select(models.Asset.symbol)).all()}
    assert {"HYPE", "DANGER", "USDC"} <= symbols
    assert int(session.scalar(select(func.count()).select_from(models.Holder))) == 2
    assert int(session.scalar(select(func.count()).select_from(models.MarketSnapshot))) == 6
    flag = session.scalar(
        select(models.ContractFlag).where(models.ContractFlag.flag_type == "mint_or_freeze_danger")
    )
    assert flag is not None and flag.severity == "critical"
    assert int(session.scalar(select(func.count()).select_from(models.Score))) >= 1
    session.close()
