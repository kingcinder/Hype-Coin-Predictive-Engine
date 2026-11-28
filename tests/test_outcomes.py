"""Outcome labels must be bounded to their prediction's observation window.

Regression coverage for the red-team finding that ``evaluate_outcomes`` used an
unbounded "latest lifecycle event" lookup: a token that collapsed after its
window ended was labelled collapsed, leaking post-window knowledge into band
calibration, the validation metrics, and the ensemble feedback loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from common.time import ensure_utc
from risk_engine.outcomes import (
    UNRESOLVED_OUTCOME_COMPONENT,
    _lifecycle_timelines,
    evaluate_outcomes,
    record_risk_outcome,
)
from storage import models
from storage.repository import insert_market_snapshot_once
from tests.conftest import seed_market_asset, seed_reference

SCORED_AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
WINDOW_HOURS = 48.0
WINDOW_END = SCORED_AT + timedelta(hours=WINDOW_HOURS)
EVAL_TS = SCORED_AT + timedelta(days=5)


def _seed_score(
    session,
    asset: models.Asset,
    *,
    decision_ts: datetime = SCORED_AT,
    model_version: str = "test-outcomes",
) -> models.Score:
    score = models.Score(
        asset_id=asset.id,
        decision_ts=decision_ts,
        observed_at=decision_ts,
        hype=0.5,
        ethos=0.5,
        risk=90.0,
        liquidity_access=0.5,
        manipulation=0.1,
        confidence=80.0,
        uncertainty=0.2,
        catalyst=0.3,
        exit_risk=0.9,
        research_priority=0.5,
        risk_band="BLACK",
        model_version=model_version,
    )
    session.add(score)
    session.flush()
    return score


def _seed_lifecycle(
    session, asset: models.Asset, phase: str, ts: datetime
) -> models.LifecycleEvent:
    event = models.LifecycleEvent(
        asset_id=asset.id,
        phase=phase,
        event_type="phase_transition",
        ts=ts,
        observed_at=ts,
        confidence=0.9,
        details={},
    )
    session.add(event)
    session.flush()
    return event


def _seed_forward_snapshot(
    session, asset: models.Asset, *, ts: datetime, price: float = 1.10
) -> None:
    """One in-window market snapshot: the forward price evidence a survival
    label requires (a survival label without it is censored, not survived)."""
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert pair is not None and source is not None
    insert_market_snapshot_once(
        session,
        pair_id=pair.id,
        source_id=source.id,
        ts=ts,
        observed_at=ts,
        price_usd=price,
        volume_usd=50_000,
        buys=30,
        sells=5,
        trades=35,
    )
    session.flush()


def _seed_prediction(
    session,
    asset: models.Asset,
    *,
    decision_ts: datetime = SCORED_AT,
    model_version: str = "test-outcomes",
) -> models.RiskOutcome:
    score = _seed_score(session, asset, decision_ts=decision_ts, model_version=model_version)
    record_risk_outcome(
        session,
        asset_id=asset.id,
        risk_band="BLACK",
        score_id=score.id,
        decision_ts=decision_ts,
    )
    outcome = session.scalar(
        select(models.RiskOutcome).where(models.RiskOutcome.score_id == score.id)
    )
    assert outcome is not None
    return outcome


def test_lifecycle_timelines_load_more_than_500_assets(session) -> None:
    """The batched timeline read must not drop assets after the first 500."""
    chain, _source = seed_reference(session)
    assets = [
        models.Asset(
            chain_id=chain.id,
            address=f"BulkTimeline{index:04d}",
            symbol=f"BULK{index:04d}",
            first_seen_at=SCORED_AT,
        )
        for index in range(501)
    ]
    session.add_all(assets)
    session.flush()
    session.add_all(
        models.LifecycleEvent(
            asset_id=asset.id,
            phase="seeding",
            event_type="phase_transition",
            ts=SCORED_AT,
            observed_at=SCORED_AT,
            confidence=0.9,
            details={},
        )
        for asset in assets
    )
    session.flush()

    timelines = _lifecycle_timelines(
        session,
        {asset.id for asset in assets},
        as_of=SCORED_AT + timedelta(minutes=1),
    )

    assert len(assets) == 501
    assert len(timelines) == len(assets)
    for asset in assets:
        times, phases = timelines[asset.id]
        assert times == [SCORED_AT]
        assert phases == ["seeding"]


def test_post_window_collapse_does_not_label_prediction(session) -> None:
    """Score Day 0, window ends Day 2, collapse Day 4 -> censored, not collapsed."""
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(days=4))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.collapsed is False
    assert outcome.survived is False
    assert outcome.evaluated_at is None
    assert outcome.censored_at is not None
    assert outcome.lifecycle_phase_at_eval == "unknown"


def test_terminal_backfill_inside_window_recovers_censored_outcome(session) -> None:
    """A late terminal event inside the window must reopen a censored row."""
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    # First evaluation sees only a non-terminal phase inside the window, so
    # the prediction is censored rather than labelled.
    _seed_lifecycle(session, asset, "ignition", SCORED_AT + timedelta(hours=6))

    first = evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert first.censored == 1
    assert outcome.evaluated_at is None
    assert outcome.censored_at is not None
    assert outcome.lifecycle_phase_at_eval == "ignition"

    # Backfill a terminal event inside the original 48h observation window.
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))
    recovery_ts = EVAL_TS + timedelta(hours=1)

    second = evaluate_outcomes(session, decision_ts=recovery_ts, window_hours=WINDOW_HOURS)

    assert second.censored == 0
    assert outcome.censored_at is None
    assert ensure_utc(outcome.evaluated_at) == recovery_ts
    assert outcome.collapsed is True
    assert outcome.rugged is False
    assert outcome.survived is False
    assert outcome.lifecycle_phase_at_eval == "collapse"


def test_collapse_inside_window_labels_collapsed(session) -> None:
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.collapsed is True
    assert outcome.rugged is False
    assert outcome.survived is False
    assert ensure_utc(outcome.evaluated_at) == EVAL_TS
    assert outcome.lifecycle_phase_at_eval == "collapse"


def test_rug_inside_window_labels_rugged_and_collapsed(session) -> None:
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "rugged", SCORED_AT + timedelta(hours=12))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.rugged is True
    assert outcome.collapsed is True
    assert outcome.survived is False
    assert ensure_utc(outcome.evaluated_at) == EVAL_TS


def test_survival_phase_at_window_end_labels_survived(session) -> None:
    """A survival phase labels survived only with forward price evidence."""
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "survivor", SCORED_AT + timedelta(hours=30))
    _seed_forward_snapshot(session, asset, ts=SCORED_AT + timedelta(hours=24))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.survived is True
    assert outcome.collapsed is False
    assert outcome.rugged is False
    assert ensure_utc(outcome.evaluated_at) == EVAL_TS
    assert outcome.lifecycle_phase_at_eval == "survivor"


def test_survival_phase_without_forward_price_is_censored(session) -> None:
    """Nov 18 mechanism: a survival phase with no forward price data must be
    censored (unevaluated), never survived.  The collapse detector never had
    data to observe a collapse, so "no collapse event" is evidence of nothing.
    """
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "survivor", SCORED_AT + timedelta(hours=30))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.collapsed is False
    assert outcome.survived is False
    assert outcome.evaluated_at is None
    assert outcome.censored_at is not None
    assert outcome.lifecycle_phase_at_eval == "survivor"


def test_saturation_phase_without_forward_price_is_censored(session) -> None:
    """The Nov 18 live rows ended at saturation with no forward snapshots;
    they must be censored, not survived."""
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "saturation", SCORED_AT + timedelta(hours=30))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.collapsed is False
    assert outcome.survived is False
    assert outcome.evaluated_at is None
    assert outcome.censored_at is not None
    assert outcome.lifecycle_phase_at_eval == "saturation"


def test_saturation_phase_with_forward_price_labels_survived(session) -> None:
    """The same saturation phase WITH a forward snapshot is a real survival."""
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "saturation", SCORED_AT + timedelta(hours=30))
    _seed_forward_snapshot(session, asset, ts=SCORED_AT + timedelta(hours=24))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.survived is True
    assert outcome.collapsed is False
    assert outcome.rugged is False
    assert ensure_utc(outcome.evaluated_at) == EVAL_TS
    assert outcome.lifecycle_phase_at_eval == "saturation"


def test_collapse_without_forward_price_still_labels_collapsed(session) -> None:
    """A collapse event is positive evidence: it labels collapsed even with
    no forward snapshots (the scan saw the data when it emitted the event)."""
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.collapsed is True
    assert outcome.survived is False
    assert ensure_utc(outcome.evaluated_at) == EVAL_TS
    assert outcome.lifecycle_phase_at_eval == "collapse"


def test_no_lifecycle_events_in_window_stays_unresolved(session) -> None:
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert outcome.collapsed is False
    assert outcome.survived is False
    assert outcome.evaluated_at is None
    assert outcome.lifecycle_phase_at_eval == "unknown"


def test_price_change_ignores_snapshots_after_window_end(session) -> None:
    """The last in-window snapshot is the exit price; later ones are ignored."""
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "survivor", SCORED_AT + timedelta(hours=40))
    pair = session.scalar(select(models.Pair).where(models.Pair.base_asset_id == asset.id))
    source = session.scalar(select(models.Source).where(models.Source.name == "dexscreener"))
    assert pair is not None
    assert source is not None

    # seed_market_asset's last entry snapshot is 1.10 at SCORED_AT.
    for hours, price in ((24, 2.20), (72, 11.0)):
        ts = SCORED_AT + timedelta(hours=hours)
        insert_market_snapshot_once(
            session,
            pair_id=pair.id,
            source_id=source.id,
            ts=ts,
            observed_at=ts,
            price_usd=price,
            volume_usd=50_000,
            buys=30,
            sells=5,
            trades=35,
        )
    session.flush()

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    # (2.20 - 1.10) / 1.10; the 11.0 post-window snapshot must not be used.
    assert outcome.price_change_pct == pytest.approx(1.0)


def test_evaluated_outcomes_commit_in_batches(session, monkeypatch) -> None:
    """A large due backlog must not hold one write transaction to the end."""
    from sqlalchemy import event

    from risk_engine import outcomes as outcomes_module

    monkeypatch.setattr(outcomes_module, "OUTCOME_BATCH_SIZE", 1)
    first = seed_market_asset(session)
    first_outcome = _seed_prediction(session, first)
    _seed_lifecycle(session, first, "collapse", SCORED_AT + timedelta(hours=24))

    second = seed_market_asset(
        session,
        address="BatchSecond111111111111111111111111111111",
        pair_address="BatchSecondPair1111111111111111111111111",
    )
    second_outcome = _seed_prediction(session, second, model_version="test-batch-second")
    _seed_lifecycle(session, second, "survivor", SCORED_AT + timedelta(hours=24))
    _seed_forward_snapshot(session, second, ts=SCORED_AT + timedelta(hours=24))

    commits: list[int] = []
    event.listen(session, "after_commit", lambda _session: commits.append(1))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert ensure_utc(first_outcome.evaluated_at) == EVAL_TS
    assert ensure_utc(second_outcome.evaluated_at) == EVAL_TS
    assert len(commits) >= 2


def test_ensemble_feedback_commit_is_visible_and_unlocked_before_calibration(
    session, monkeypatch
) -> None:
    """Calibration must start after the scan commits its ensemble markers.

    ``run_calibration`` calls ``evaluate_outcomes`` first.  A scanner that
    leaves the ``ensemble_fed_at`` writes uncommitted would both hide them from
    another connection and keep SQLite's writer lock while calibration starts.
    The calibration hook below runs immediately after the real evaluation,
    before calibration writes anything, and checks both conditions.
    """
    import sqlite3

    from risk_engine import calibrator
    from scoring.ensemble import ensemble_engine

    monkeypatch.setattr(ensemble_engine, "record_outcomes", lambda _entries: None)
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))
    session.commit()

    db_path = session.get_bind().url.database
    assert db_path

    observed: dict[str, object] = {}
    real_evaluate = calibrator.evaluate_outcomes

    def evaluate_then_probe(*args, **kwargs):
        report = real_evaluate(*args, **kwargs)

        # A separate connection proves the marker was committed by the scan
        # session, not just left in that session's identity map.
        reader = sqlite3.connect(db_path, timeout=1.0)
        try:
            row = reader.execute(
                "select count(*) from risk_outcomes where id = ? and ensemble_fed_at is not null",
                (outcome.id,),
            ).fetchone()
        finally:
            reader.close()
        observed["committed_marker"] = row[0] if row else 0

        # BEGIN IMMEDIATE fails immediately if the scan session still owns the
        # SQLite writer lock; keep the probe bounded instead of waiting out the
        # production busy timeout.
        writer = sqlite3.connect(db_path, timeout=0.2)
        try:
            writer.execute("pragma busy_timeout=100")
            writer.execute("begin immediate")
            observed["writer_lock_available"] = True
        finally:
            writer.rollback()
            writer.close()
        return report

    monkeypatch.setattr(calibrator, "evaluate_outcomes", evaluate_then_probe)

    calibrator.run_calibration(session, decision_ts=EVAL_TS)

    assert observed["committed_marker"] == 1
    assert observed["writer_lock_available"] is True


def test_ensemble_feedback_failure_does_not_refeed_committed_rows(session, monkeypatch) -> None:
    """A mid-loop failure must not replay outcomes whose weights were saved.

    The ensemble persists weights through its own session.  If the marker for
    an earlier row is only written after the whole loop, a later
    ``record_outcomes`` failure discards that marker and the next evaluation
    feeds the earlier outcome again.  Markers must be durable per row before
    the next row is attempted.
    """
    import sqlite3

    from scoring.ensemble import ensemble_engine

    first = seed_market_asset(session)
    first_outcome = _seed_prediction(session, first)
    _seed_lifecycle(session, first, "collapse", SCORED_AT + timedelta(hours=24))

    second = seed_market_asset(
        session,
        address="EnsembleRetrySecond1111111111111111111111",
        pair_address="EnsembleRetrySecondPair111111111111111",
    )
    second_outcome = _seed_prediction(session, second, model_version="test-ensemble-retry-second")
    _seed_lifecycle(session, second, "collapse", SCORED_AT + timedelta(hours=24))
    session.commit()

    fed_calls: list[list[dict[str, object]]] = []

    def fail_second_feedback(entries: list[dict[str, object]]) -> None:
        fed_calls.append(entries)
        if len(fed_calls) == 2:
            raise RuntimeError("ensemble feedback failed after the first row")

    monkeypatch.setattr(ensemble_engine, "record_outcomes", fail_second_feedback)

    # The first row is fed and marked; the second row fails before its marker.
    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert len(fed_calls) == 2
    session.refresh(first_outcome)
    session.refresh(second_outcome)
    assert [first_outcome.ensemble_fed_at, second_outcome.ensemble_fed_at].count(None) == 1

    # Read through a separate connection: the surviving marker must have been
    # committed by the failed evaluation, not merely flushed in this session.
    db_path = session.get_bind().url.database
    assert db_path
    reader = sqlite3.connect(db_path, timeout=1.0)
    try:
        committed = reader.execute(
            "select count(*) from risk_outcomes where id in (?, ?) and ensemble_fed_at is not null",
            (first_outcome.id, second_outcome.id),
        ).fetchone()[0]
    finally:
        reader.close()
    assert committed == 1

    # A retry must feed only the row that did not complete, not the first one.
    fed_calls.clear()
    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert len(fed_calls) == 1
    session.refresh(first_outcome)
    session.refresh(second_outcome)
    assert first_outcome.ensemble_fed_at is not None
    assert second_outcome.ensemble_fed_at is not None


def test_large_due_backlog_uses_bounded_batches_and_short_lock_holds(session) -> None:
    """A realistic due backlog must commit every 500 rows, not hold one lock.

    The independent probe is a second SQLite connection that repeatedly asks
    for the writer lock.  Its longest blocked interval is the observed lock
    hold; the elapsed time covers the whole outcome-evaluation pass.
    """
    import sqlite3
    import threading
    import time

    from sqlalchemy import event

    from risk_engine import outcomes as outcomes_module

    backlog = 5000
    assert outcomes_module.OUTCOME_BATCH_SIZE == 500
    asset = seed_market_asset(session)
    scores = [
        models.Score(
            asset_id=asset.id,
            decision_ts=SCORED_AT,
            observed_at=SCORED_AT,
            hype=0.5,
            ethos=0.5,
            risk=90.0,
            liquidity_access=0.5,
            manipulation=0.1,
            confidence=80.0,
            uncertainty=0.2,
            catalyst=0.3,
            exit_risk=0.9,
            research_priority=0.5,
            risk_band="BLACK",
            model_version=f"test-large-backlog-{index}",
        )
        for index in range(backlog)
    ]
    session.add_all(scores)
    session.flush()
    session.add_all(
        [
            models.RiskOutcome(
                asset_id=asset.id,
                score_id=score.id,
                risk_band="BLACK",
                scored_at=SCORED_AT,
                lifecycle_phase_at_score="seeding",
                label_version="v2_bounded",
                details={},
            )
            for score in scores
        ]
    )
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))
    session.commit()

    commits: list[int] = []
    write_started: float | None = None
    write_holds: list[float] = []

    @event.listens_for(session.get_bind(), "before_cursor_execute")
    def _mark_write(  # type: ignore[no-untyped-def]
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal write_started
        if write_started is None and statement.lstrip().upper().startswith(
            ("UPDATE", "INSERT", "DELETE")
        ):
            write_started = time.perf_counter()

    @event.listens_for(session, "after_commit")
    def _finish_commit(_session) -> None:  # type: ignore[no-untyped-def]
        nonlocal write_started
        commits.append(1)
        if write_started is not None:
            write_holds.append(time.perf_counter() - write_started)
            write_started = None

    db_path = str(session.get_bind().url.database)
    assert db_path
    stop = threading.Event()
    ready = threading.Event()
    holds: list[float] = []

    def probe_writer_lock() -> None:
        connection = sqlite3.connect(db_path, timeout=0.005)
        connection.execute("PRAGMA busy_timeout=5")
        blocked_since: float | None = None
        ready.set()
        try:
            while not stop.is_set():
                attempt = time.monotonic()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower():
                        if blocked_since is None:
                            blocked_since = attempt
                        time.sleep(0.002)
                        continue
                    raise
                if blocked_since is not None:
                    holds.append(attempt - blocked_since)
                    blocked_since = None
                connection.execute("ROLLBACK")
                time.sleep(0.002)
        finally:
            if blocked_since is not None:
                holds.append(time.monotonic() - blocked_since)
            connection.close()

    thread = threading.Thread(target=probe_writer_lock, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    started = time.perf_counter()
    try:
        report = evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)
    finally:
        elapsed = time.perf_counter() - started
        stop.set()
        thread.join(timeout=5)

    max_lock_hold = max(holds, default=0.0)
    max_write_txn = max(write_holds, default=0.0)
    assert write_holds, "no write transaction was observed"
    print(
        "large_outcome_backlog "
        f"rows={backlog} elapsed={elapsed:.3f}s max_probe_lock={max_lock_hold:.3f}s "
        f"max_write_txn={max_write_txn:.3f}s commits={len(commits)}"
    )

    assert report.total_flagged == backlog
    assert report.total_collapsed == backlog
    # One commit at each 500-row boundary plus the final health/ensemble writes.
    assert len(commits) >= 10
    assert elapsed < 60.0, f"outcome evaluation took {elapsed:.1f}s"
    assert max_lock_hold < 10.0, f"writer lock held for {max_lock_hold:.1f}s"
    assert max_write_txn < 10.0, f"write transaction held for {max_write_txn:.1f}s"


def test_censored_backlog_uses_one_update_per_batch(session, monkeypatch) -> None:
    """Unlabelable due rows must be retired with set-based updates, not one
    UPDATE per ORM object, so a 40k-row age-out cannot hold the writer lock."""
    from sqlalchemy import event

    from risk_engine import outcomes as outcomes_module

    monkeypatch.setattr(outcomes_module, "OUTCOME_BATCH_SIZE", 3)
    asset = seed_market_asset(session)
    for index in range(6):
        _seed_prediction(session, asset, model_version=f"test-censor-batch-{index}")

    updates: list[str] = []

    @event.listens_for(session.get_bind(), "before_cursor_execute")
    def _record_update(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("UPDATE RISK_OUTCOMES"):
            updates.append(statement)

    report = evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert report.censored == 6
    assert len(updates) == 2


def test_report_censors_rows_without_a_label_in_their_window(session) -> None:
    asset = seed_market_asset(session)
    withheld = _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(days=4))
    # A later prediction whose 48h window has not elapsed yet is not unresolved.
    # It needs a fresh snapshot at its own score time: the v3 freshness gate
    # skips outcomes with no market data within 24h of scoring.
    _seed_forward_snapshot(session, asset, ts=SCORED_AT + timedelta(days=4))
    _seed_prediction(session, asset, decision_ts=SCORED_AT + timedelta(days=4))

    resolved_asset = seed_market_asset(
        session,
        address="Resolved111111111111111111111111111111111",
        pair_address="ResolvedPair11111111111111111111111111111",
    )
    _seed_prediction(session, resolved_asset, model_version="test-resolved")
    _seed_lifecycle(session, resolved_asset, "collapse", SCORED_AT + timedelta(hours=24))

    report = evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert withheld.evaluated_at is None
    assert withheld.censored_at is not None
    assert report.unresolved == 0
    assert report.censored == 1

    # A censored row is retired instead of retried on every later pass.
    report_again = evaluate_outcomes(
        session, decision_ts=EVAL_TS + timedelta(days=1), window_hours=WINDOW_HOURS
    )
    assert report_again.unresolved == 0
    assert report_again.censored == 0


def test_censored_outcomes_clear_the_health_backlog(session) -> None:
    """Unlabelable due rows are retired instead of pinning health yellow/red."""
    asset = seed_market_asset(session)
    _seed_prediction(session, asset)
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(days=4))

    held = seed_market_asset(
        session,
        address="Held111111111111111111111111111111111111",
        pair_address="HeldPair11111111111111111111111111111111",
    )
    _seed_lifecycle(session, held, "collapse", SCORED_AT + timedelta(days=4))

    def _health_rows() -> list[models.SystemHealth]:
        return list(
            session.scalars(
                select(models.SystemHealth)
                .where(models.SystemHealth.component == UNRESOLVED_OUTCOME_COMPONENT)
                .order_by(models.SystemHealth.ts.asc())
            )
        )

    report = evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)
    assert report.unresolved == 0
    assert report.censored == 1
    rows = _health_rows()
    assert rows[-1].error_count == 0
    assert rows[-1].state == "ok"
    assert "censored=1" in (rows[-1].message or "")

    # A second unlabelable prediction is censored too, not accumulated.
    _seed_prediction(session, held, model_version="test-held")
    report = evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)
    assert report.unresolved == 0
    assert report.censored == 1
    rows = _health_rows()
    assert rows[-1].error_count == 0
    assert rows[-1].state == "ok"
    assert "GREW" not in (rows[-1].message or "")


def test_window_end_boundary_is_inclusive_and_one_second_later_is_not(session) -> None:
    inside = seed_market_asset(
        session,
        address="BoundaryInside1111111111111111111111111111",
        pair_address="BoundaryInsidePair111111111111111111111111",
    )
    after = seed_market_asset(
        session,
        address="BoundaryAfter11111111111111111111111111111",
        pair_address="BoundaryAfterPair1111111111111111111111111",
    )
    inside_outcome = _seed_prediction(session, inside, model_version="test-boundary-inside")
    after_outcome = _seed_prediction(session, after, model_version="test-boundary-after")
    _seed_lifecycle(session, inside, "collapse", WINDOW_END)
    _seed_lifecycle(session, after, "collapse", WINDOW_END + timedelta(seconds=1))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert inside_outcome.collapsed is True
    assert ensure_utc(inside_outcome.evaluated_at) == EVAL_TS
    assert after_outcome.collapsed is False
    assert after_outcome.evaluated_at is None
    assert after_outcome.censored_at is not None
    assert after_outcome.lifecycle_phase_at_eval == "unknown"


def test_legacy_row_with_existing_bounded_row_does_not_break_evaluation(session) -> None:
    """Regression: relabelling a superseded v1 row crashed the whole scan.

    ``evaluate_outcomes`` stamps the current label version when it evaluates a
    row. If the append-only relabel already wrote a bounded row for the same
    score, that UPDATE violates ``uq_risk_outcomes_score_label``; the scan
    rolled back and left a red worker health row. The bounded row must be
    evaluated and the superseded v1 row left untouched.
    """
    asset = seed_market_asset(session)
    bounded_row = _seed_prediction(session, asset, model_version="test-legacy-dup")
    score_id = bounded_row.score_id
    legacy = models.RiskOutcome(
        asset_id=asset.id,
        score_id=score_id,
        risk_band="BLACK",
        scored_at=SCORED_AT,
        lifecycle_phase_at_score="unknown",
        label_version="v1_unbounded",
        details={},
    )
    session.add(legacy)
    session.flush()
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert bounded_row.collapsed is True
    assert ensure_utc(bounded_row.evaluated_at) == EVAL_TS
    assert bounded_row.label_version == "v3_bounded"
    assert legacy.evaluated_at is None
    assert legacy.label_version == "v1_unbounded"
    rows = session.scalars(
        select(models.RiskOutcome).where(models.RiskOutcome.score_id == score_id)
    ).all()
    assert len(rows) == 2


def test_evaluated_outcome_is_labelled_v3_bounded(session) -> None:
    """A freshly evaluated outcome carries the bounded label version."""
    from risk_engine.outcomes import LABEL_VERSION

    assert LABEL_VERSION == "v3_bounded"
    asset = seed_market_asset(session)
    outcome = _seed_prediction(session, asset, model_version="test-label-ver")
    _seed_lifecycle(session, asset, "collapse", SCORED_AT + timedelta(hours=24))

    evaluate_outcomes(session, decision_ts=EVAL_TS, window_hours=WINDOW_HOURS)

    assert ensure_utc(outcome.evaluated_at) == EVAL_TS
    assert outcome.label_version == "v3_bounded"
