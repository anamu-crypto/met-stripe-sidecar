"""Handlers for Stripe ``customer.created`` and ``customer.updated`` events.

Out-of-order handling
---------------------
Stripe does not guarantee webhook ordering. We treat both events as upserts:

  - ``customer.created`` for an already-mapped customer is a no-op (we log,
    and trust the existing mapping).
  - ``customer.updated`` for an unknown customer falls through to the create
    path, using the update payload as the source of truth.

Both paths end at the same state: a row in ``customer_mappings`` and a customer
in Metronome whose name matches the latest Stripe payload. This avoids the
"retry until create arrives" stall mode entirely.

Failure classification
----------------------
The two exception types in :mod:`sidecar.handlers.errors` —
``RetryableHandlerError`` and ``PermanentHandlerError`` — travel up to the
worker. The worker schedules a retry for retryable and marks the row ``failed``
for permanent. Anything unclassified bubbles up as an unexpected exception,
which the worker also treats as retryable (safer default).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sidecar.config import Settings
from sidecar.handlers.errors import PermanentHandlerError, RetryableHandlerError
from sidecar.logging import get_logger
from sidecar.mappers.customer import MapperError, stripe_customer_to_metronome_request
from sidecar.metronome_client import (
    MetronomeClient,
    PermanentMetronomeError,
    TransientMetronomeError,
)
from sidecar.models import CustomerMapping

logger = get_logger(__name__)


class HandlerOutcome(StrEnum):
    """What the handler did. Used in structured logs and tests."""

    CREATED = "created"
    UPDATED = "updated"
    SKIPPED_ALREADY_MAPPED = "skipped_already_mapped"


@dataclass(frozen=True, slots=True)
class HandlerResult:
    outcome: HandlerOutcome
    stripe_customer_id: str
    metronome_customer_id: str
    duration_ms: int


# -----------------------------------------------------------------------------
# Public entrypoints
# -----------------------------------------------------------------------------


async def handle_customer_created(
    *,
    session: AsyncSession,
    metronome: MetronomeClient,
    settings: Settings,  # noqa: ARG001 — accepted for uniform dispatch signature
    stripe_event_id: str,
    event_payload: dict[str, Any],
) -> HandlerResult:
    """Handle a ``customer.created`` Stripe event.

    ``settings`` is accepted but unused; v0.1 customer handling does not read
    config beyond what's already bound to the Metronome client. It's part of
    the uniform dispatch signature shared with
    :func:`sidecar.handlers.subscription.handle_subscription_created` so the
    worker can call every handler the same way.
    """
    return await _handle_customer_event(
        session=session,
        metronome=metronome,
        stripe_event_id=stripe_event_id,
        event_payload=event_payload,
        event_type="customer.created",
    )


async def handle_customer_updated(
    *,
    session: AsyncSession,
    metronome: MetronomeClient,
    settings: Settings,  # noqa: ARG001 — accepted for uniform dispatch signature
    stripe_event_id: str,
    event_payload: dict[str, Any],
) -> HandlerResult:
    """Handle a ``customer.updated`` Stripe event.

    See :func:`handle_customer_created` for why ``settings`` is accepted but
    unused.
    """
    return await _handle_customer_event(
        session=session,
        metronome=metronome,
        stripe_event_id=stripe_event_id,
        event_payload=event_payload,
        event_type="customer.updated",
    )


# -----------------------------------------------------------------------------
# Shared implementation
# -----------------------------------------------------------------------------


async def _handle_customer_event(
    *,
    session: AsyncSession,
    metronome: MetronomeClient,
    stripe_event_id: str,
    event_payload: dict[str, Any],
    event_type: str,
) -> HandlerResult:
    started_at = time.perf_counter()
    stripe_customer = _extract_customer_object(event_payload)
    stripe_customer_id = _require_str(stripe_customer.get("id"), "data.object.id")

    log_ctx: dict[str, Any] = {
        "event": "handler_started",
        "stripe_event_id": stripe_event_id,
        "stripe_event_type": event_type,
        "stripe_customer_id": stripe_customer_id,
    }
    logger.info("handler_started", extra=log_ctx)

    existing = await _lookup_mapping(session, stripe_customer_id)

    if existing is not None:
        result = await _handle_existing_mapping(
            session=session,
            metronome=metronome,
            stripe_customer=stripe_customer,
            mapping=existing,
            event_type=event_type,
            started_at=started_at,
        )
    else:
        result = await _create_new_mapping(
            session=session,
            metronome=metronome,
            stripe_customer=stripe_customer,
            started_at=started_at,
        )

    logger.info(
        "handler_completed",
        extra={
            "event": "handler_completed",
            "stripe_event_id": stripe_event_id,
            "stripe_event_type": event_type,
            "stripe_customer_id": result.stripe_customer_id,
            "metronome_customer_id": result.metronome_customer_id,
            "outcome": result.outcome.value,
            "duration_ms": result.duration_ms,
        },
    )
    return result


async def _handle_existing_mapping(
    *,
    session: AsyncSession,
    metronome: MetronomeClient,
    stripe_customer: dict[str, Any],
    mapping: CustomerMapping,
    event_type: str,
    started_at: float,
) -> HandlerResult:
    """Mapping already exists. Update Metronome name on update events; no-op otherwise."""
    if event_type == "customer.created":
        # Out-of-order or duplicate delivery. Don't recreate; trust the mapping.
        return HandlerResult(
            outcome=HandlerOutcome.SKIPPED_ALREADY_MAPPED,
            stripe_customer_id=mapping.stripe_customer_id,
            metronome_customer_id=mapping.metronome_customer_id,
            duration_ms=_elapsed_ms(started_at),
        )

    # event_type == "customer.updated": propagate the latest name to Metronome.
    try:
        request = stripe_customer_to_metronome_request(stripe_customer)
    except MapperError as exc:
        raise PermanentHandlerError(f"Cannot map Stripe customer: {exc}") from exc

    try:
        await metronome.set_customer_name(
            metronome_customer_id=mapping.metronome_customer_id,
            name=request["name"],
        )
    except TransientMetronomeError as exc:
        raise RetryableHandlerError(str(exc)) from exc
    except PermanentMetronomeError as exc:
        raise PermanentHandlerError(str(exc)) from exc

    mapping.updated_at = datetime.now(UTC)
    await session.flush()

    return HandlerResult(
        outcome=HandlerOutcome.UPDATED,
        stripe_customer_id=mapping.stripe_customer_id,
        metronome_customer_id=mapping.metronome_customer_id,
        duration_ms=_elapsed_ms(started_at),
    )


async def _create_new_mapping(
    *,
    session: AsyncSession,
    metronome: MetronomeClient,
    stripe_customer: dict[str, Any],
    started_at: float,
) -> HandlerResult:
    """No mapping yet. Create the Metronome customer and persist the mapping."""
    stripe_customer_id = _require_str(stripe_customer.get("id"), "data.object.id")

    try:
        request = stripe_customer_to_metronome_request(stripe_customer)
    except MapperError as exc:
        raise PermanentHandlerError(f"Cannot map Stripe customer: {exc}") from exc

    try:
        created = await metronome.create_customer(request)
    except TransientMetronomeError as exc:
        raise RetryableHandlerError(str(exc)) from exc
    except PermanentMetronomeError as exc:
        raise PermanentHandlerError(str(exc)) from exc

    mapping = CustomerMapping(
        stripe_customer_id=stripe_customer_id,
        metronome_customer_id=created.metronome_customer_id,
        updated_at=datetime.now(UTC),
    )
    session.add(mapping)
    await session.flush()

    return HandlerResult(
        outcome=HandlerOutcome.CREATED,
        stripe_customer_id=stripe_customer_id,
        metronome_customer_id=created.metronome_customer_id,
        duration_ms=_elapsed_ms(started_at),
    )


# -----------------------------------------------------------------------------
# Small helpers — kept here rather than in a shared util to keep the handler
# self-contained and easy to read in isolation.
# -----------------------------------------------------------------------------


async def _lookup_mapping(
    session: AsyncSession, stripe_customer_id: str
) -> CustomerMapping | None:
    stmt = select(CustomerMapping).where(
        CustomerMapping.stripe_customer_id == stripe_customer_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _extract_customer_object(event_payload: dict[str, Any]) -> dict[str, Any]:
    """Pull ``data.object`` out of a Stripe Event payload defensively."""
    data = event_payload.get("data")
    if not isinstance(data, dict):
        raise PermanentHandlerError("Stripe event missing `data` object.")
    obj = data.get("object")
    if not isinstance(obj, dict):
        raise PermanentHandlerError("Stripe event missing `data.object`.")
    return obj


def _require_str(value: Any, field_path: str) -> str:
    if not isinstance(value, str) or not value:
        raise PermanentHandlerError(f"Stripe event field `{field_path}` is missing or empty.")
    return value


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)
