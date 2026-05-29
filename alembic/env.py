"""Alembic migration environment.

We run migrations synchronously (psycopg2 / sync URL) even though the app uses
asyncpg. This is the standard Alembic pattern and avoids the complexity of
running Alembic's online migrations through an async engine.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from sidecar.models import Base


def _load_dotenv_if_present() -> None:
    """Read a ``.env`` next to ``alembic.ini`` into ``os.environ`` (idempotent).

    Mirrors what pydantic-settings does for the app at runtime, so
    ``alembic upgrade head`` works locally without exporting variables by hand.
    Variables already present in the environment win.
    """
    candidate = Path(__file__).resolve().parent.parent / ".env"
    if not candidate.is_file():
        return
    for raw in candidate.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv_if_present()

# This is the Alembic Config object, which provides access to values within
# the .ini file in use.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """Resolve a synchronous SQLAlchemy URL for migrations.

    The app speaks asyncpg (`postgresql+asyncpg://...`). Alembic uses sync
    drivers, so we strip the `+asyncpg` qualifier and let SQLAlchemy pick the
    default sync driver (psycopg2).
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is required to run migrations. "
            "Set it in your environment or .env file."
        )
    # Normalize to a sync driver. psycopg2 is installed implicitly via
    # SQLAlchemy's default; if not available, install with `pip install psycopg2-binary`.
    return url.replace("+asyncpg", "")


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no DB connection)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect and apply)."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
