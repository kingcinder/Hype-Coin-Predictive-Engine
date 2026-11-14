"""Tests for ledger schema tables."""

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from storage.database import Base
from storage.models import (
    LedgerOpenPosition,
)


def test_ledger_tables_exist():
    """All ledger tables should be creatable."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    from sqlalchemy import inspect

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    ledger_tables = [
        "ledger_open_positions",
        "ledger_trades",
        "ledger_review_acks",
        "ledger_scorecards",
        "price_evidence",
    ]

    for table in ledger_tables:
        assert table in tables, f"Table {table} not found"


def test_ledger_open_position_columns():
    """LedgerOpenPosition should have expected columns."""
    from sqlalchemy import inspect

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("ledger_open_positions")}

    expected = {
        "id",
        "mode",
        "mint",
        "chain",
        "source_signal",
        "entry_quote_price",
        "entry_effective_price",
        "qty",
        "size_sol",
        "entry_fees_sol",
        "entry_slip_pct",
        "is_new_token",
        "stop_pct",
        "target_pct",
        "time_stop_minutes",
        "stop_price",
        "target_price",
        "time_stop_at",
        "opened_at",
        "price_source",
        "price_source_ts",
        "candidate_evidence_ts",
        "why_entered",
        "rules_sha256",
        "created_at",
    }
    assert expected.issubset(columns), f"Missing columns: {expected - columns}"


def test_ledger_trade_columns():
    """LedgerTrade should have expected columns."""
    from sqlalchemy import inspect

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("ledger_trades")}

    expected = {
        "id",
        "mode",
        "opened_at",
        "closed_at",
        "mint",
        "chain",
        "source_signal",
        "entry_price",
        "size_sol",
        "fees_sol",
        "slippage_sol",
        "stop_pct",
        "target_pct",
        "time_stop_minutes",
        "exit_price",
        "pnl_sol",
        "tx_signature",
        "why_entered",
        "why_exited",
        "lesson",
        "candidate_evidence_ts",
        "price_source",
        "price_source_ts",
        "rules_sha256",
        "correction_of",
        "created_at",
    }
    assert expected.issubset(columns), f"Missing columns: {expected - columns}"


def test_ledger_review_ack_columns():
    """LedgerReviewAck should have expected columns."""
    from sqlalchemy import inspect

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("ledger_review_acks")}

    expected = {"id", "acked_at", "note"}
    assert expected.issubset(columns), f"Missing columns: {expected - columns}"


def test_ledger_scorecard_columns():
    """LedgerScorecard should have expected columns."""
    from sqlalchemy import inspect

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("ledger_scorecards")}

    expected = {"id", "created_at", "mode", "payload"}
    assert expected.issubset(columns), f"Missing columns: {expected - columns}"


def test_price_evidence_columns():
    """PriceEvidence should have expected columns."""
    from sqlalchemy import inspect

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("price_evidence")}

    expected = {
        "id",
        "mint",
        "chain",
        "side",
        "price",
        "price_source",
        "source_ts",
        "fetched_at",
        "trade_ref",
    }
    assert expected.issubset(columns), f"Missing columns: {expected - columns}"


def test_ledger_open_position_insert():
    """Should be able to insert and query a LedgerOpenPosition."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    now = datetime.now(UTC)
    with Session(engine) as session:
        pos = LedgerOpenPosition(
            mode="paper",
            mint="SOLUSD",
            chain="solana",
            source_signal="ignition:1:1",
            entry_quote_price=100.0,
            entry_effective_price=99.5,
            qty=1.0,
            size_sol=1.0,
            entry_fees_sol=0.001,
            entry_slip_pct=0.5,
            is_new_token=False,
            stop_pct=5.0,
            target_pct=10.0,
            time_stop_minutes=120,
            stop_price=95.0,
            target_price=110.0,
            time_stop_at=None,
            opened_at=now,
            price_source="oracle",
            price_source_ts=now,
            candidate_evidence_ts=now,
            why_entered="test entry",
            rules_sha256="abc123",
            created_at=now,
        )
        session.add(pos)
        session.commit()

        result = session.query(LedgerOpenPosition).first()
        assert result is not None
        assert result.mode == "paper"
        assert result.mint == "SOLUSD"
