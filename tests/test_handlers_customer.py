"""Integration tests for the customer handlers with Metronome HTTP-mocked.

We mock Metronome at the *HTTP* layer using ``respx``. The Metronome SDK uses
``httpx`` under the hood, so this exercises the real SDK code path including
header construction, JSON serialization, and our error classification.

The database is a real Postgres; the ``db_session`` fixture provides a clean
slate for each test.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sidecar.config import get_settings
from sidecar.handlers import (
    PermanentHandlerError,
    RetryableHandlerError,
    handle_customer_created,
    handle_customer_updated,
)
from sidecar.handlers.customer import HandlerOutcome
from sidecar.metronome_client import MetronomeClient
from sidecar.models import CustomerMapping

METRONOME_CREATE_URL = "https://api.metronome.test/v1/customers"


def _customer_response(*, metronome_customer_id: str, name: str = "Acme, Inc.") -> dict:
    """Build a response body matching the Metronome SDK's `Customer` shape."""
    return {
        "data": {
            "id": metronome_customer_id,
            "external_id": metronome_customer_id,
            "ingest_aliases": [metronome_customer_id],
            "name": name,
        }
    }


def _metronome_client() -> MetronomeClient:
    settings = get_settings()
    return MetronomeClient(
        api_key=settings.metronome_api_key.get_secret_value(),
        base_url=settings.metronome_base_url,
    )


def _set_name_url(metronome_customer_id: str) -> str:
    return f"https://api.metronome.test/v1/customers/{metronome_customer_id}/setName"


# -----------------------------------------------------------------------------
# Happy paths
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_customer_created_happy_path(
    db_session: AsyncSession,
    customer_created_event: dict[str, Any],
) -> None:
    metronome_customer_id = "mc_001"

    with respx.mock(assert_all_called=True) as mock:
        create_route = mock.post(METRONOME_CREATE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_customer_response(metronome_customer_id=metronome_customer_id),
            )
        )

        metronome = _metronome_client()
        try:
            result = await handle_customer_created(
                session=db_session,
                metronome=metronome,
                settings=get_settings(),
                stripe_event_id=customer_created_event["id"],
                event_payload=customer_created_event,
            )
        finally:
            await metronome.aclose()

    assert result.outcome == HandlerOutcome.CREATED
    assert result.stripe_customer_id == "cus_TEST123"
    assert result.metronome_customer_id == metronome_customer_id

    # Mapping row written.
    await db_session.commit()
    mapping = await _get_mapping(db_session, "cus_TEST123")
    assert mapping is not None
    assert mapping.metronome_customer_id == metronome_customer_id

    # Request body matches what the mapper produced.
    sent = json.loads(create_route.calls.last.request.content)
    assert sent["name"] == "Acme, Inc."
    assert sent["ingest_aliases"] == ["cus_TEST123"]
    cfg = sent["customer_billing_provider_configurations"][0]
    assert cfg["billing_provider"] == "stripe"
    assert cfg["configuration"]["stripe_customer_id"] == "cus_TEST123"


@pytest.mark.asyncio
async def test_handle_customer_updated_updates_existing_mapping(
    db_session: AsyncSession,
    customer_created_event: dict[str, Any],
) -> None:
    """customer.updated for a known customer calls setName on Metronome."""
    db_session.add(
        CustomerMapping(
            stripe_customer_id="cus_TEST123",
            metronome_customer_id="mc_existing",
        )
    )
    await db_session.flush()

    # Make it look like an update.
    update_event = {**customer_created_event, "type": "customer.updated"}
    update_event["data"]["object"]["name"] = "Acme Renamed, Inc."

    with respx.mock(assert_all_called=True) as mock:
        set_name_route = mock.post(_set_name_url("mc_existing")).mock(
            return_value=httpx.Response(
                200,
                json=_customer_response(
                    metronome_customer_id="mc_existing", name="Acme Renamed, Inc."
                ),
            )
        )

        metronome = _metronome_client()
        try:
            result = await handle_customer_updated(
                session=db_session,
                metronome=metronome,
                settings=get_settings(),
                stripe_event_id="evt_update",
                event_payload=update_event,
            )
        finally:
            await metronome.aclose()

    assert result.outcome == HandlerOutcome.UPDATED
    assert result.metronome_customer_id == "mc_existing"
    sent = json.loads(set_name_route.calls.last.request.content)
    # customer_id travels in the URL path, not the body; only `name` is in the body.
    assert sent == {"name": "Acme Renamed, Inc."}


# -----------------------------------------------------------------------------
# Idempotency / already-mapped
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_created_is_noop_when_already_mapped(
    db_session: AsyncSession,
    customer_created_event: dict[str, Any],
) -> None:
    """Receiving customer.created a second time must NOT recreate in Metronome."""
    db_session.add(
        CustomerMapping(
            stripe_customer_id="cus_TEST123",
            metronome_customer_id="mc_existing",
        )
    )
    await db_session.flush()

    with respx.mock(assert_all_called=False) as mock:
        # Register the endpoint so respx would intercept it — but assert it
        # was never called.
        create_route = mock.post(METRONOME_CREATE_URL).mock(
            return_value=httpx.Response(500, json={"error": "should not be called"})
        )

        metronome = _metronome_client()
        try:
            result = await handle_customer_created(
                session=db_session,
                metronome=metronome,
                settings=get_settings(),
                stripe_event_id=customer_created_event["id"],
                event_payload=customer_created_event,
            )
        finally:
            await metronome.aclose()

    assert result.outcome == HandlerOutcome.SKIPPED_ALREADY_MAPPED
    assert result.metronome_customer_id == "mc_existing"
    assert not create_route.called, "Metronome create must not be called when already mapped."


# -----------------------------------------------------------------------------
# Error classification
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metronome_4xx_raises_permanent(
    db_session: AsyncSession,
    customer_created_event: dict[str, Any],
) -> None:
    """A 4xx (other than 429) from Metronome surfaces as PermanentHandlerError."""
    with respx.mock() as mock:
        mock.post(METRONOME_CREATE_URL).mock(
            return_value=httpx.Response(400, json={"message": "bad request"}),
        )

        metronome = _metronome_client()
        try:
            with pytest.raises(PermanentHandlerError):
                await handle_customer_created(
                    session=db_session,
                    metronome=metronome,
                settings=get_settings(),
                    stripe_event_id=customer_created_event["id"],
                    event_payload=customer_created_event,
                )
        finally:
            await metronome.aclose()

    # No mapping row was written.
    assert await _get_mapping(db_session, "cus_TEST123") is None


@pytest.mark.asyncio
async def test_metronome_5xx_raises_transient(
    db_session: AsyncSession,
    customer_created_event: dict[str, Any],
) -> None:
    """A 5xx from Metronome surfaces as RetryableHandlerError (worker retries)."""
    with respx.mock() as mock:
        mock.post(METRONOME_CREATE_URL).mock(
            return_value=httpx.Response(503, json={"message": "down"}),
        )

        metronome = _metronome_client()
        try:
            with pytest.raises(RetryableHandlerError):
                await handle_customer_created(
                    session=db_session,
                    metronome=metronome,
                settings=get_settings(),
                    stripe_event_id=customer_created_event["id"],
                    event_payload=customer_created_event,
                )
        finally:
            await metronome.aclose()


@pytest.mark.asyncio
async def test_metronome_429_raises_transient(
    db_session: AsyncSession,
    customer_created_event: dict[str, Any],
) -> None:
    with respx.mock() as mock:
        mock.post(METRONOME_CREATE_URL).mock(
            return_value=httpx.Response(429, json={"message": "slow down"}),
        )

        metronome = _metronome_client()
        try:
            with pytest.raises(RetryableHandlerError):
                await handle_customer_created(
                    session=db_session,
                    metronome=metronome,
                settings=get_settings(),
                    stripe_event_id=customer_created_event["id"],
                    event_payload=customer_created_event,
                )
        finally:
            await metronome.aclose()


# -----------------------------------------------------------------------------
# Out-of-order handling: customer.updated arrives before customer.created.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_updated_for_unknown_customer_creates(
    db_session: AsyncSession,
    customer_created_event: dict[str, Any],
) -> None:
    """customer.updated for a customer we've never seen should upsert."""
    update_event = {**customer_created_event, "type": "customer.updated"}

    with respx.mock(assert_all_called=True) as mock:
        mock.post(METRONOME_CREATE_URL).mock(
            return_value=httpx.Response(
                200, json=_customer_response(metronome_customer_id="mc_via_update")
            )
        )

        metronome = _metronome_client()
        try:
            result = await handle_customer_updated(
                session=db_session,
                metronome=metronome,
                settings=get_settings(),
                stripe_event_id="evt_update_first",
                event_payload=update_event,
            )
        finally:
            await metronome.aclose()

    assert result.outcome == HandlerOutcome.CREATED
    mapping = await _get_mapping(db_session, "cus_TEST123")
    assert mapping is not None
    assert mapping.metronome_customer_id == "mc_via_update"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _get_mapping(
    session: AsyncSession, stripe_customer_id: str
) -> CustomerMapping | None:
    stmt = select(CustomerMapping).where(
        CustomerMapping.stripe_customer_id == stripe_customer_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()
