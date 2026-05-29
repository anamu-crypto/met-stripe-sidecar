"""Integration tests for :func:`handle_subscription_created`.

Same shape as ``test_handlers_customer.py``: real Postgres via the
``db_session`` fixture, Metronome HTTP mocked at the wire layer with ``respx``.
The Metronome SDK uses ``httpx`` underneath, so this exercises real header
construction, JSON serialization, and the sidecar's error-classification path.

Three behaviors are covered, chosen because each one corresponds to a
production failure mode that the v0.2a design specifically prevents:

  1. **Dedupe**: a worker retry after a partial commit must not re-create the
     contract on Metronome. The handler short-circuits on the local mapping.
  2. **Out-of-order**: a subscription event arriving before its customer event
     must surface as ``RetryableHandlerError`` so the worker backs off and
     retries — instead of crashing on the FK to ``customer_mappings``.
  3. **409 recovery**: if Metronome already created the contract for this
     ``uniqueness_key`` (because a previous attempt succeeded on the API but
     failed before persisting locally), the handler recovers the existing
     contract ID via the ``contracts.list`` lookup rather than failing.

If you fork this repo and modify the handler, these are the three tests you
most want to keep green.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sidecar.config import get_settings
from sidecar.config.tiers import TIERS
from sidecar.handlers import RetryableHandlerError, handle_subscription_created
from sidecar.handlers.subscription import HandlerOutcome
from sidecar.metronome_client import MetronomeClient
from sidecar.models import CustomerMapping, SubscriptionMapping

# Metronome endpoints the SDK calls. Pinned to the test base URL set in
# tests/conftest.py so respx intercepts them.
METRONOME_CONTRACT_CREATE_URL = "https://api.metronome.test/v1/contracts/create"
METRONOME_CONTRACT_LIST_URL = "https://api.metronome.test/v1/contracts/list"

STRIPE_CUSTOMER_ID = "cus_TEST123"
METRONOME_CUSTOMER_ID = "11111111-1111-1111-1111-111111111111"


def _contract_create_response(metronome_contract_id: str) -> dict:
    """Match the SDK's ``ContractCreateResponse`` shape (``data.id``)."""
    return {"data": {"id": metronome_contract_id}}


def _contract_list_response(
    *, metronome_contract_id: str, uniqueness_key: str
) -> dict:
    """Single-item ``contracts.list`` response — what the 409 recovery reads."""
    return {
        "data": [
            {
                "id": metronome_contract_id,
                "uniqueness_key": uniqueness_key,
            }
        ]
    }


def _metronome_client() -> MetronomeClient:
    settings = get_settings()
    return MetronomeClient(
        api_key=settings.metronome_api_key.get_secret_value(),
        base_url=settings.metronome_base_url,
    )


async def _seed_customer_mapping(session: AsyncSession) -> None:
    """Insert the parent customer mapping the subscription handler requires."""
    session.add(
        CustomerMapping(
            stripe_customer_id=STRIPE_CUSTOMER_ID,
            metronome_customer_id=METRONOME_CUSTOMER_ID,
        )
    )
    await session.flush()


async def _get_subscription_mapping(
    session: AsyncSession, stripe_subscription_id: str
) -> SubscriptionMapping | None:
    stmt = select(SubscriptionMapping).where(
        SubscriptionMapping.stripe_subscription_id == stripe_subscription_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# -----------------------------------------------------------------------------
# 1. Dedupe — worker retry after a partial commit must not re-create.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_already_mapped_skips_metronome(
    db_session: AsyncSession,
    subscription_created_event: dict[str, Any],
) -> None:
    """A second delivery of the same subscription event is a local no-op.

    Metronome must not be called: the local ``subscription_mappings`` row is
    sufficient evidence that the work has been done.
    """
    await _seed_customer_mapping(db_session)
    db_session.add(
        SubscriptionMapping(
            stripe_subscription_id="sub_TEST123",
            stripe_customer_id=STRIPE_CUSTOMER_ID,
            metronome_contract_id="contract_existing",
            current_tier_name="startup",
            current_stripe_price_id="price_REPLACE_ME_startup",
        )
    )
    await db_session.flush()

    # Register the create endpoint so respx would intercept it, then assert
    # below it was never called. Mirrors the customer-handler test pattern.
    with respx.mock(assert_all_called=False) as mock:
        create_route = mock.post(METRONOME_CONTRACT_CREATE_URL).mock(
            return_value=httpx.Response(
                500, json={"error": "should not be called"}
            )
        )

        metronome = _metronome_client()
        try:
            result = await handle_subscription_created(
                session=db_session,
                metronome=metronome,
                settings=get_settings(),
                stripe_event_id=subscription_created_event["id"],
                event_payload=subscription_created_event,
            )
        finally:
            await metronome.aclose()

    assert result.outcome == HandlerOutcome.SKIPPED_ALREADY_MAPPED
    assert result.metronome_contract_id == "contract_existing"
    assert result.metronome_customer_id == METRONOME_CUSTOMER_ID
    assert not create_route.called, (
        "Metronome contracts.create must not be called when the subscription "
        "already has a local mapping row."
    )


# -----------------------------------------------------------------------------
# 2. Out-of-order — subscription event before its parent customer event.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_out_of_order_raises_retryable(
    db_session: AsyncSession,
    subscription_created_event: dict[str, Any],
) -> None:
    """No customer mapping → RetryableHandlerError, no Metronome call.

    Stripe does not guarantee webhook ordering, so this case is routine: the
    worker must back off and retry once ``customer.created`` lands. We
    explicitly DO NOT want the handler to FK-violate at the database layer
    (the resulting Postgres traceback would be misleading), nor to call
    Metronome (the contract has no customer to attach to).
    """
    with respx.mock(assert_all_called=False) as mock:
        create_route = mock.post(METRONOME_CONTRACT_CREATE_URL).mock(
            return_value=httpx.Response(200, json={"data": {"id": "should_not"}})
        )

        metronome = _metronome_client()
        try:
            with pytest.raises(RetryableHandlerError, match="out-of-order"):
                await handle_subscription_created(
                    session=db_session,
                    metronome=metronome,
                    settings=get_settings(),
                    stripe_event_id=subscription_created_event["id"],
                    event_payload=subscription_created_event,
                )
        finally:
            await metronome.aclose()

    assert not create_route.called, (
        "Metronome contracts.create must not be called when the parent "
        "customer mapping does not exist yet."
    )
    assert (
        await _get_subscription_mapping(db_session, "sub_TEST123") is None
    ), "No subscription mapping row may be persisted on the out-of-order path."


# -----------------------------------------------------------------------------
# 3. 409 recovery — Metronome already has the contract; recover its ID.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_409_recovers_existing_contract(
    db_session: AsyncSession,
    subscription_created_event: dict[str, Any],
) -> None:
    """Metronome 409 → recover via uniqueness_key, mark outcome as recovered.

    Failure mode being prevented: a previous attempt successfully created the
    contract on Metronome but crashed before persisting the local mapping.
    On retry, ``POST /v1/contracts/create`` returns 409 because the
    deterministic ``uniqueness_key`` collides. The client lists contracts on
    the customer, finds the matching key, and returns the existing
    ``metronome_contract_id`` — the handler persists the mapping as if the
    create had just succeeded.

    The test pulls a price ID from the live :data:`TIERS` dict so it runs
    correctly against any fork's configured tiers — hard-coding the
    placeholder price would fail the moment the fork edits ``tiers.py``.
    """
    await _seed_customer_mapping(db_session)

    # Pick any configured tier and patch the fixture so the handler resolves it.
    price_id, tier = next(iter(TIERS.items()))
    subscription_created_event["data"]["object"]["items"]["data"][0]["price"][
        "id"
    ] = price_id

    # Pre-compute the uniqueness_key the mapper will emit:
    # ``{sub_id}_{price_id}_{period_start_floored_to_hour}``. The fixture's
    # period_start (1767225600 == 2026-01-01T00:00:00Z) is already on the
    # hour so flooring is a no-op for this test.
    expected_uniqueness_key = f"sub_TEST123_{price_id}_1767225600"
    recovered_contract_id = "22222222-2222-2222-2222-222222222222"

    with respx.mock(assert_all_called=True) as mock:
        create_route = mock.post(METRONOME_CONTRACT_CREATE_URL).mock(
            return_value=httpx.Response(
                409,
                json={"message": "uniqueness_key collision"},
            )
        )
        # Metronome's contracts list endpoint is POST /v1/contracts/list with
        # the customer_id in the JSON body, not GET with a query param.
        list_route = mock.post(METRONOME_CONTRACT_LIST_URL).mock(
            return_value=httpx.Response(
                200,
                json=_contract_list_response(
                    metronome_contract_id=recovered_contract_id,
                    uniqueness_key=expected_uniqueness_key,
                ),
            )
        )

        metronome = _metronome_client()
        try:
            result = await handle_subscription_created(
                session=db_session,
                metronome=metronome,
                settings=get_settings(),
                stripe_event_id=subscription_created_event["id"],
                event_payload=subscription_created_event,
            )
        finally:
            await metronome.aclose()

    assert result.outcome == HandlerOutcome.CREATED_VIA_UNIQUENESS_KEY_RECOVERY
    assert result.metronome_contract_id == recovered_contract_id

    # Mapping row was persisted with the recovered ID — handler treats this
    # path identically to a fresh create, by design.
    await db_session.commit()
    mapping = await _get_subscription_mapping(db_session, "sub_TEST123")
    assert mapping is not None
    assert mapping.metronome_contract_id == recovered_contract_id
    assert mapping.current_tier_name == tier.name
    assert mapping.current_stripe_price_id == price_id

    assert create_route.called
    assert list_route.called, (
        "On 409 the client must list contracts to recover the existing ID."
    )
