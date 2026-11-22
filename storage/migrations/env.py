from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from common.config import get_settings
from storage import models  # noqa: F401 registers metadata
from storage.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _ensure_version_table_capacity(connectable) -> None:
    """Widen alembic_version.version_num before Alembic stamps a version.

    Alembic creates the version table with version_num VARCHAR(32), but
    several revision ids in this repo are longer (0039 is 39 chars, 0041
    is 40), so stamping them fails with "value too long for type
    character varying(32)" on a fresh database. Pre-create/widen the
    column to VARCHAR(255) on PostgreSQL. Other dialects (e.g. SQLite in
    unit tests) do not enforce VARCHAR lengths, so they need nothing.
    """
    with connectable.begin() as conn:
        if conn.dialect.name != "postgresql":
            return
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(255) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        conn.execute(text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"))


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    _ensure_version_table_capacity(connectable)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
