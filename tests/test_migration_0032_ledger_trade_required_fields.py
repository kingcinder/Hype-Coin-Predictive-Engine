"""0032 replaces nullable ledger_trades contract fields with required ones."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from common.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
REVISION_0031 = "0031_whale_telemetry"
REVISION_0032 = "0032_ledger_trade_required_fields"


def _alembic_config() -> Config:
    config = Config(str(ROOT / "storage" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "storage" / "migrations"))
    config.set_main_option("path_separator", "os")
    return config


def _create_legacy_table(database: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE ledger_trades (
                    id INTEGER PRIMARY KEY,
                    stop_pct NUMERIC(10, 4),
                    why_entered TEXT,
                    why_exited TEXT,
                    lesson TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO ledger_trades (id, stop_pct, why_entered, why_exited, lesson)
                VALUES (1, NULL, NULL, NULL, NULL),
                       (2, NULL, 'kept entry', NULL, 'kept lesson'),
                       (3, 0.05, 'existing entry', 'existing exit', 'existing lesson')
                """
            )
        )
    engine.dispose()


def _not_null_columns(database: Path) -> dict[str, bool]:
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(ledger_trades)")).mappings().all()
    finally:
        engine.dispose()
    return {str(row["name"]): bool(row["notnull"]) for row in rows}


def _rows(database: Path) -> dict[int, dict[str, object]]:
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    try:
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        """
                        SELECT id, stop_pct, why_entered, why_exited, lesson
                        FROM ledger_trades ORDER BY id
                        """
                    )
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    return {int(row["id"]): dict(row) for row in rows}


def _insert_null_row(database: Path, row_id: int) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO ledger_trades (id, stop_pct, why_entered, why_exited, lesson)
                    VALUES (:id, NULL, NULL, NULL, NULL)
                    """
                ),
                {"id": row_id},
            )
    finally:
        engine.dispose()


def test_0032_backfills_legacy_rows_and_round_trips(tmp_path, monkeypatch) -> None:
    database = tmp_path / "legacy.db"
    _create_legacy_table(database)
    monkeypatch.setenv("SERPENT_DB_PATH", str(database))
    get_settings.cache_clear()
    config = _alembic_config()

    try:
        command.stamp(config, REVISION_0031)
        command.upgrade(config, REVISION_0032)

        flags = _not_null_columns(database)
        for column in ("stop_pct", "why_entered", "why_exited", "lesson"):
            assert flags[column] is True, column

        rows = _rows(database)
        assert Decimal(str(rows[1]["stop_pct"])) == 0
        assert rows[1]["why_entered"] == ""
        assert rows[1]["why_exited"] == ""
        assert rows[1]["lesson"] == ""
        assert rows[2]["why_entered"] == "kept entry"
        assert rows[2]["why_exited"] == ""
        assert rows[2]["lesson"] == "kept lesson"
        assert Decimal(str(rows[3]["stop_pct"])) == Decimal("0.05")
        assert rows[3]["why_entered"] == "existing entry"
        assert rows[3]["why_exited"] == "existing exit"
        assert rows[3]["lesson"] == "existing lesson"

        engine = create_engine(f"sqlite+pysqlite:///{database}")
        try:
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO ledger_trades (id, stop_pct, why_entered, why_exited, lesson)
                        VALUES (4, NULL, 'entry', 'exit', 'lesson')
                        """
                    )
                )
        finally:
            engine.dispose()

        command.downgrade(config, REVISION_0031)
        downgraded = _not_null_columns(database)
        for column in ("stop_pct", "why_entered", "why_exited", "lesson"):
            assert downgraded[column] is False, column

        _insert_null_row(database, 10)
        command.upgrade(config, REVISION_0032)

        upgraded_again = _not_null_columns(database)
        for column in ("stop_pct", "why_entered", "why_exited", "lesson"):
            assert upgraded_again[column] is True, column
        assert set(_rows(database)) == {1, 2, 3, 10}
        assert Decimal(str(_rows(database)[10]["stop_pct"])) == 0
        assert _rows(database)[10]["why_entered"] == ""
        assert _rows(database)[10]["why_exited"] == ""
        assert _rows(database)[10]["lesson"] == ""
    finally:
        get_settings.cache_clear()
