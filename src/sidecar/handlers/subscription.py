"""Handler for the Stripe ``customer.subscription.created`` event.

Companion to :mod:`sidecar.handlers.customer`. Follows the same lifecycle:

  1. Pull ``data.object`` out of the Stripe event.
  2. Reject obviously-malformed payloads (multi-item, missing fields) up
     front so the operator-facing error message is precise.
  3. Look up the local mappings we need (customer, prior subscription).
  4. Resolve the tier from the config module.
  5. Build the Metronome contract request via the pure mapper.
  6. Call Metronome (with built-in 409 recovery).
  7. Persist the subscription mapping.

Out-of-order handling
---------------------
``customer.subscription.created`` can arrive at the receiver before
``customer.created`` has been processed by the worker, because Stripe does
not guarantee webhook ordering. When that happens, the FK from
``subscription_mappings`` to ``customer_mappings`` would fail at insert time
anyway — but the handler checks earlier and raises
:class:`RetryableHandlerError` so the worker retries with exponential backoff.
That's strictly easier to interpret in the logs than a Postgres FK-violation
traceback.

Idempotency
-----------
There are two layers:

  * The receiver dedupes by ``stripe_event_id`` (Stripe redelivery).
  * The handler dedupes by ``stripe_subscription_id`` (worker retries).

Both layers exist because they catch different failure modes — Stripe retrying
the same delivery vs. our worker retrying after a partial success — and
because the second layer also keeps us correct against the small chance that
Stripe issues two *different* ``customer.subscription.created`` events for the
same subscription (it shouldn't, but defensive is cheap here).

If the local mapping check is a no-op (handler returns
:attr:`HandlerOutcome.SKIPPED_ALREADY_MAPPED`) we do **not** call Metronome.
That's important: the Metronome side is already correct, and a redundant API
call would only burn rate-limit budget.
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
from sidecar.config.tiers import Tier, UnknownTierError, lookup_tier
from sidecar.handlers.errors import PermanentHandlerError, RetryableHandlerError
from sidecar.logging import get_logger
from sidecar.mappers.subscription import (
    stripe_subscription_to_metronome_contract_request,
)
from sidecar.metronome_client import (
    MetronomeClient,
    PermanentMetronomeError,
    TransientMetronomeError,
)
from sidecar.models import CustomerMapping, SubscriptionMapping

logger = get_logger(__name__)


class HandlerOutcome(StrEnum):
    """What the handler did. Used in structured logs and tests."""

    CREATED = "created"
    CREATED_VIA_UNIQUENESS_KEY_RECOVERY = "created_via_uniqueness_key_recovery"
    SKIPPED_ALREADY_MAPPED = "skipped_already_mapped"


@dataclass(frozen=True, slots=True)
class HandlerResult:
    outcome: HandlerOutcome
    stripe_subscription_id: str
    stripe_customer_id: str
    metronome_customer_id: str
    metronome_contract_id: str
    tier_name: str
    duration_ms: int


# -----------------------------------------------------------------------------
# Public entrypoint
# -----------------------------------------------------------------------------


async def handle_subscription_created(
    *,
    session: AsyncSession,
    metronome: MetronomeClient,
    settings: Settings,
    stripe_event_id: str,
    event_payload: dict[str, Any],
) -> HandlerResult:
    """Handle a ``customer.subscription.created`` Stripe event."""
    started_at = time.perf_counter()
    stripe_subscription = _extract_subscription_object(event_payload)

    stripe_subscription_id = _require_str(stripe_subscription.get("id"), "data.object.id")
    stripe_customer_id = _require_str(
        stripe_subscription.get("customer"), "data.object.customer"
    )

    log_ctx: dict[str, Any] = {
        "stripe_event_id": stripe_event_id,
        "stripe_event_type": "customer.subscription.created",
        "stripe_subscription_id": stripe_subscription_id,
        "stripe_customer_id": stripe_customer_id,
    }
    logger.info("handler_started", extra={"event": "handler_started", **log_ctx})

    # Step 1: dedupe against a prior successful run of this same handler.
    existing_mapping = await _lookup_subscription_mapping(session, stripe_subscription_id)
    if existing_mapping is not None:
        metronome_customer_id = await _require_metronome_customer_id(
            session, existing_mapping.stripe_customer_id
        )
        result = HandlerResult(
            outcome=HandlerOutcome.SKIPPED_ALREADY_MAPPED,
            stripe_subscription_id=existing_mapping.stripe_subscription_id,
            stripe_customer_id=existing_mapping.stripe_customer_id,
            metronome_customer_id=metronome_customer_id,
            metronome_contract_id=existing_mapping.metronome_contract_id,
            tier_name=existing_mapping.current_tier_name,
            duration_ms=_elapsed_ms(started_at),
        )
        _log_completed(result, log_ctx)
        return result

    # Step 2: parse + reject multi-item early — we want this rejection to
    # happen *before* we hit the database for the customer mapping lookup
    # (the v0.2a handler-test spec checks "no Metronome call AND no extra
    # DB churn before the multi-item rejection").
    price_id = _extract_single_price_id(stripe_subscription)

    # Step 3: customer mapping must exist. If not, this event arrived out of
    # order with customer.created; raise retryable so the worker tries again
    # after exponential backoff.
    customer_mapping = await _lookup_customer_mapping(session, stripe_customer_id)
    if customer_mapping is None:
        raise RetryableHandlerError(
            f"No customer_mappings row for Stripe customer "
            f"{stripe_customer_id!r}. Likely an out-of-order webhook — "
            f"customer.created has not been processed yet."
        )

    # Step 4: resolve tier from config.
    try:
        tier = lookup_tier(price_id)
    except UnknownTierError as exc:
        raise PermanentHandlerError(str(exc)) from exc

    # Step 5: build the Metronome request (pure function).
    try:
        request_body = stripe_subscription_to_metronome_contract_request(
            stripe_subscription=stripe_subscription,
            tier=tier,
            metronome_customer_id=customer_mapping.metronome_customer_id,
            rate_card_id=settings.metronome_default_rate_card_id,
        )
    except ValueError as exc:
        raise PermanentHandlerError(f"Cannot map Stripe subscription: {exc}") from exc

    # Step 6: call Metronome. The client handles 409 recovery internally.
    try:
        created = await metronome.create_contract(request_body)
    except TransientMetronomeError as exc:
        raise RetryableHandlerError(str(exc)) from exc
    except PermanentMetronomeError as exc:
        raise PermanentHandlerError(str(exc)) from exc

    # Step 7: persist the mapping. The FK to customer_mappings is enforced by
    # the DB, but we've already verified it above.
    mapping = SubscriptionMapping(
        stripe_subscription_id=stripe_subscription_id,
        stripe_customer_id=stripe_customer_id,
        metronome_contract_id=created.metronome_contract_id,
        current_tier_name=tier.name,
        current_stripe_price_id=price_id,
        updated_at=datetime.now(UTC),
    )
    session.add(mapping)
    await session.flush()

    outcome = (
        HandlerOutcome.CREATED_VIA_UNIQUENESS_KEY_RECOVERY
        if created.via_uniqueness_key_recovery
        else HandlerOutcome.CREATED
    )
    result = HandlerResult(
        outcome=outcome,
        stripe_subscription_id=stripe_subscription_id,
        stripe_customer_id=stripe_customer_id,
        metronome_customer_id=customer_mapping.metronome_customer_id,
        metronome_contract_id=created.metronome_contract_id,
        tier_name=tier.name,
        duration_ms=_elapsed_ms(started_at),
    )
    _log_completed(result, log_ctx)
    return result


# -----------------------------------------------------------------------------
# Small helpers — kept here so the handler reads top-to-bottom in isolation.
# -----------------------------------------------------------------------------


async def _lookup_subscription_mapping(
    session: AsyncSession, stripe_subscription_id: str
) -> SubscriptionMapping | None:
    stmt = select(SubscriptionMapping).where(
        SubscriptionMapping.stripe_subscription_id == stripe_subscription_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _lookup_customer_mapping(
    session: AsyncSession, stripe_customer_id: str
) -> CustomerMapping | None:
    stmt = select(CustomerMapping).where(
        CustomerMapping.stripe_customer_id == stripe_customer_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _require_metronome_customer_id(
    session: AsyncSession, stripe_customer_id: str
) -> str:
    """Resolve the metronome_customer_id for the already-mapped customer.

    Only called on the "skipped — already mapped" path, where the customer
    mapping is guaranteed to exist by the FK. If it doesn't, something has
    corrupted the database; raise loudly so an operator sees it.
    """
    mapping = await _lookup_customer_mapping(session, stripe_customer_id)
    if mapping is None:
        raise PermanentHandlerError(
            f"subscription_mappings row exists for Stripe customer "
            f"{stripe_customer_id!r} but no customer_mappings row does — "
            f"database integrity issue."
        )
    return mapping.metronome_customer_id


def _extract_subscription_object(event_payload: dict[str, Any]) -> dict[str, Any]:
    """Pull ``data.object`` out of a Stripe Event payload defensively."""
    data = event_payload.get("data")
    if not isinstance(data, dict):
        raise PermanentHandlerError("Stripe event missing `data` object.")
    obj = data.get("object")
    if not isinstance(obj, dict):
        raise PermanentHandlerError("Stripe event missing `data.object`.")
    return obj


def _extract_single_price_id(stripe_subscription: dict[str, Any]) -> str:
    """Return the price ID for a single-item subscription.

    Raises PermanentHandlerError on multi-item (out of scope for v0.2a) or
    malformed payloads. We deliberately mirror the mapper's multi-item check
    here so the rejection happens before any DB lookup or Metronome call —
    matching the test that asserts "no Metronome call for multi-item".
    """
    items = stripe_subscription.get("items")
    if not isinstance(items, dict):
        raise PermanentHandlerError(
            "Stripe subscription is missing `items`; cannot determine tier."
        )
    data = items.get("data")
    if not isinstance(data, list) or len(data) == 0:
        raise PermanentHandlerError(
            "Stripe subscription has no items; cannot determine tier."
        )
    if len(data) > 1:
        raise PermanentHandlerError(
            f"Stripe subscription has {len(data)} items; multi-item "
            "subscriptions are not supported in v0.2a."
        )
    item = data[0]
    if not isinstance(item, dict):
        raise PermanentHandlerError(
            "Stripe subscription `items.data[0]` is not an object."
        )
    price = item.get("price")
    if not isinstance(price, dict):
        raise PermanentHandlerError(
            "Stripe subscription item is missing `price`."
        )
    price_id = price.get("id")
    if not isinstance(price_id, str) or not price_id:
        raise PermanentHandlerError(
            "Stripe subscription item is missing `price.id`."
        )
    return price_id


def _require_str(value: Any, field_path: str) -> str:
    if not isinstance(value, str) or not value:
        raise PermanentHandlerError(
            f"Stripe event field `{field_path}` is missing or empty."
        )
    return value


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _log_completed(result: HandlerResult, ctx: dict[str, Any]) -> None:
    logger.info(
        "handler_completed",
        extra={
            "event": "handler_completed",
            **ctx,
            "metronome_customer_id": result.metronome_customer_id,
            "metronome_contract_id": result.metronome_contract_id,
            "tier_name": result.tier_name,
            "outcome": result.outcome.value,
            "duration_ms": result.duration_ms,
        },
    )


async def handle_subscription_updated(
    *,
    session: AsyncSession,  # noqa: ARG001 — kept for handler-signature uniformity
    metronome: MetronomeClient,  # noqa: ARG001
    settings: Settings,  # noqa: ARG001
    stripe_event_id: str,
    event_payload: dict[str, Any],
) -> None:
    """Loud no-op for ``customer.subscription.updated``.

    v0.2a is **create-only**. Tier upgrades / downgrades, plan swaps, quantity
    changes, and any other mid-life subscription edits are not yet propagated
    to the Metronome contract. We register this handler explicitly (rather
    than letting the event fall through ``event_ignored_unknown_type``) so:

      * The log line names the integrity gap (`metronome_contract_not_amended`)
        instead of looking like a benign "ignored unknown type".
      * Operators querying ``webhook_events`` for this type see ``processed``
        and don't waste time investigating "stuck" events.
      * Anyone reading the worker registry can see at a glance that we are
        consciously not handling these events — not just that we forgot.

    See README "What's not handled (yet)" for the v0.2b scope that closes this.
    """
    stripe_subscription = _extract_subscription_object(event_payload)
    stripe_subscription_id = _require_str(
        stripe_subscription.get("id"), "data.object.id"
    )
    stripe_customer_id = _require_str(
        stripe_subscription.get("customer"), "data.object.customer"
    )
    logger.warning(
        "subscription_update_not_propagated",
        extra={
            "event": "subscription_update_not_propagated",
            "stripe_event_id": stripe_event_id,
            "stripe_event_type": "customer.subscription.updated",
            "stripe_subscription_id": stripe_subscription_id,
            "stripe_customer_id": stripe_customer_id,
            "metronome_contract_amended": False,
            "reason": (
                "v0.2a does not handle subscription updates; the Metronome "
                "contract still reflects the original tier. Implement in v0.2b."
            ),
        },
    )


async def handle_subscription_deleted(
    *,
    session: AsyncSession,  # noqa: ARG001
    metronome: MetronomeClient,  # noqa: ARG001
    settings: Settings,  # noqa: ARG001
    stripe_event_id: str,
    event_payload: dict[str, Any],
) -> None:
    """Loud no-op for ``customer.subscription.deleted``.

    v0.2a does not terminate the Metronome contract when the Stripe
    subscription is cancelled. **This means the customer continues to accrue
    Metronome credit for the remainder of the contract's billing period.**
    For most setups that's the wrong default — surface it loudly so the
    operator either implements termination (v0.2b) or wires up an external
    process to do it.

    Like :func:`handle_subscription_updated`, registering this explicitly
    keeps the data-integrity gap visible in the worker registry and in logs.
    """
    stripe_subscription = _extract_subscription_object(event_payload)
    stripe_subscription_id = _require_str(
        stripe_subscription.get("id"), "data.object.id"
    )
    stripe_customer_id = _require_str(
        stripe_subscription.get("customer"), "data.object.customer"
    )
    logger.warning(
        "subscription_deletion_not_propagated",
        extra={
            "event": "subscription_deletion_not_propagated",
            "stripe_event_id": stripe_event_id,
            "stripe_event_type": "customer.subscription.deleted",
            "stripe_subscription_id": stripe_subscription_id,
            "stripe_customer_id": stripe_customer_id,
            "metronome_contract_terminated": False,
            "reason": (
                "v0.2a does not terminate Metronome contracts on Stripe "
                "subscription cancel; the contract remains active until its "
                "natural end. Implement termination in v0.2b or run a "
                "separate cleanup process."
            ),
        },
    )


__all__ = [
    "HandlerOutcome",
    "HandlerResult",
    "handle_subscription_created",
    "handle_subscription_deleted",
    "handle_subscription_updated",
]
