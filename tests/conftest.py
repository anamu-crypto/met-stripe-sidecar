"""Shared pytest fixtures.

The tests fall into three buckets:

1. **Mapper tests** — pure functions, no I/O. Need no fixtures.
2. **Webhook receiver tests** — exercise the FastAPI app against a real
   Postgres. We mount the FastAPI app with TestClient and assert against DB rows.
3. **Handler / worker tests** — call handlers directly with a real DB session
   and a respx-mocked Metronome HTTP endpoint.

The DB-backed tests share a single Postgres database (the one in
``docker-compose.yml``) and **truncate** their tables between tests rather than
re-running migrations. This makes the suite fast while preserving the partial
indexes and constraints that ``create_all`` would skip.

Local prerequisite: ``docker compose up -d db`` (or set ``DATABASE_URL`` to a
Postgres of your choice). CI provides a Postgres service in the workflow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import NullPool

# -----------------------------------------------------------------------------
# Environment defaults — must be set before importing anything that reads them.
# -----------------------------------------------------------------------------

os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_secret_value")
os.environ.setdefault("METRONOME_API_KEY", "test-metronome-key")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://sidecar:localdev@localhost:5432/sidecar",
)
os.environ.setdefault(
    "METRONOME_DEFAULT_RATE_CARD_ID",
    "00000000-0000-0000-0000-000000000001",
)
# Make the worker's backoff predictable in tests.
os.environ.setdefault("WORKER_RETRY_BASE_SECONDS", "1")
os.environ.setdefault("WORKER_RETRY_CAP_SECONDS", "1")
os.environ.setdefault("WORKER_MAX_ATTEMPTS", "3")
os.environ.setdefault("METRONOME_BASE_URL", "https://api.metronome.test")
os.environ.setdefault("LOG_LEVEL", "WARNING")  # keep test output quiet

# Now safe to import the app — settings will pick up the env above.
from sidecar.config import get_settings
from sidecar.db import dispose_engine, get_sessionmaker, reset_engine_for_tests
from sidecar.models import Base
from sidecar.server import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# -----------------------------------------------------------------------------
# Database lifecycle
# -----------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def _engine() -> AsyncIterator[AsyncEngine]:
    """Session-wide async engine. Creates the schema on first use.

    We use ``Base.metadata.create_all`` rather than running Alembic so the test
    suite doesn't depend on having psycopg2 installed. The Alembic migration is
    exercised separately at ``docker compose up`` time and in CI.

    Uses ``NullPool`` so every checkout opens a fresh asyncpg connection.
    Pooled connections in tests cause "got Future attached to a different
    loop" errors on Python ≥ 3.13: pytest-asyncio's per-test loop, FastAPI's
    ``TestClient`` loop, and the engine's pool-pre-ping all interact badly.
    Production keeps the pooled engine — this only affects tests.
    """
    # Reset cached settings so env-var defaults set above take effect.
    get_settings.cache_clear()
    settings = get_settings()
    engine = reset_engine_for_tests(settings.database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engine()


@pytest_asyncio.fixture()
async def db_session(_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test async session against a truncated database.

    Truncates ``webhook_events``, ``customer_mappings`` and
    ``subscription_mappings`` before each test so tests are order-independent
    without paying the cost of dropping/recreating. ``CASCADE`` is required
    because of the foreign key from ``subscription_mappings`` to
    ``customer_mappings``.
    """
    sessionmaker = get_sessionmaker()
    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE webhook_events, customer_mappings, "
                "subscription_mappings RESTART IDENTITY CASCADE"
            )
        )
    async with sessionmaker() as session:
        yield session


# -----------------------------------------------------------------------------
# Stripe signature helper. Computing a real signature here means the webhook
# receiver test exercises the full SDK verification path, not a mock.
# -----------------------------------------------------------------------------


def make_stripe_signature(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """Produce a valid ``Stripe-Signature`` header for ``payload``.

    Mirrors the construction Stripe uses on the wire so we can assert the
    receiver verifies the real signed envelope, not a stub.
    """
    ts = int(time.time()) if timestamp is None else timestamp
    signed = f"{ts}.{payload.decode('utf-8')}".encode()
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


# -----------------------------------------------------------------------------
# FastAPI test client
# -----------------------------------------------------------------------------


@pytest.fixture
def app(_engine: AsyncEngine) -> FastAPI:
    """A fresh FastAPI app per test."""
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Synchronous TestClient — easier to use than httpx.AsyncClient for these tests."""
    with TestClient(app) as c:
        yield c


# -----------------------------------------------------------------------------
# Stripe event fixture
# -----------------------------------------------------------------------------


@pytest.fixture
def customer_created_event() -> dict[str, Any]:
    """Loaded copy of the canonical Stripe customer.created fixture."""
    return json.loads((FIXTURES_DIR / "stripe_customer_created.json").read_text())


@pytest.fixture
def subscription_created_event() -> dict[str, Any]:
    """Loaded copy of the canonical Stripe customer.subscription.created fixture.

    Single-item subscription whose ``data.object.items.data[0].price.id`` is
    ``price_REPLACE_ME_startup``, the default ``startup`` tier shipped in
    :mod:`sidecar.config.tiers`. Tests that need a different tier should mutate
    the dict in place.
    """
    return json.loads(
        (FIXTURES_DIR / "stripe_customer_subscription_created.json").read_text()
    )
