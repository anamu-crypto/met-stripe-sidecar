"""Database engine and session management.

We use SQLAlchemy 2.x with its native async support over asyncpg. The engine is
created lazily on first use and is a process-wide singleton.

Why a singleton: an asyncpg connection pool is expensive to create. Re-using one
engine across the FastAPI app and the worker matches how SQLAlchemy is intended
to be used and avoids "too many clients" errors in Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sidecar.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            # `pool_pre_ping` adds a cheap SELECT 1 to detect connections that
            # have been killed by the database server (e.g. after a failover).
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            # `future=True` is the default in 2.x but kept explicit for clarity.
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a transactional scope around a series of operations.

    Usage:

        async with session_scope() as session:
            session.add(some_row)
            # commit happens automatically on successful exit; rollback on error.
    """
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    """Close all pooled connections. Call on graceful shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


# Re-exported so tests can construct a fresh engine against a different URL.
def reset_engine_for_tests(url: str, **engine_kwargs: Any) -> AsyncEngine:
    """Replace the singleton engine with one pointed at `url`.

    Intended for tests only. Production code should use `get_engine()`.
    """
    global _engine, _sessionmaker
    _engine = create_async_engine(url, future=True, **engine_kwargs)
    _sessionmaker = async_sessionmaker(
        bind=_engine, expire_on_commit=False, class_=AsyncSession
    )
    return _engine
